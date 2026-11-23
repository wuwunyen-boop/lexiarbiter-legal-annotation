"""LexiArbiter main window."""

from __future__ import annotations

import logging
import os
import re
import sys
import uuid
import copy
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

from PySide6.QtCore import Qt, QPoint, QSize, QTimer, QEventLoop
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QIcon, QPainter, QPalette, QPixmap,
    QKeySequence, QShortcut,
)
from PySide6.QtGui import QUndoCommand, QUndoStack
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSplitter, QStatusBar, QToolBar, QVBoxLayout,
    QWidget, QWidgetAction,
)

from . import __app_name__, __version__
from .core import config as cfgmod
from .core import io as iomod
from .core.config import (
    AnnotationMode, GroupDef, LabelDef, UserPreferences,
    list_annotation_modes, load_annotation_mode,
)
from .core.logger import current_log_dir
from .core.models import Annotation, Document, detect_same_group_conflicts
from .widgets.editor import AnnotationEditor
from .widgets.file_panel import FilePanel
from .widgets.concept_panel import (
    ConceptEditDialog,
    ConceptSelectionDialog,
    ConceptSidebar,
)


# Autosave cadence. 60s is a good compromise — short enough that worst-case
# data loss (Qt/C++ crash, OOM, power loss) is bounded, long enough not to
# pester the disk while the user is mid-annotation.
_AUTOSAVE_INTERVAL_MS = 60_000

# Autosave 檔名規則：``<base>.autosave.<timestamp>.lexa``。
# 第二段 ``\.autosave`` 後的 ``(?:\.[^.]+)?`` 用來相容舊版無時間戳的
# ``<base>.autosave.lexa``，遷移期間還能正確辨識並一起做修剪 / 清理。
_AUTOSAVE_RE = re.compile(
    r"^(?P<base>.+)\.autosave(?:\.[^.]+)?\.lexa$",
    re.IGNORECASE,
)


def _is_autosave_name(name: str) -> bool:
    """檔名是否為 autosave 產物（含舊版無時間戳格式）。"""
    return bool(_AUTOSAVE_RE.match(name))


def _autosave_base_path(p: Path) -> Path:
    """把 autosave 檔還原成它對應的『原檔』.lexa 路徑；非 autosave 原樣返回。"""
    m = _AUTOSAVE_RE.match(p.name)
    if m:
        return p.with_name(m.group("base") + ".lexa")
    return p


class _DocumentSnapshotCommand(QUndoCommand):
    """Undo one document edit by restoring full before/after snapshots."""

    def __init__(self, window, before: Document, after: Document, label: str):
        super().__init__(label)
        self.window = window
        self.before = copy.deepcopy(before)
        self.after = copy.deepcopy(after)
        self._first_redo = True

    def undo(self):
        self.window._restore_document(self.before)

    def redo(self):
        if self._first_redo:
            self._first_redo = False
            return
        self.window._restore_document(self.after)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _swatch_icon(hex_color: Optional[str], size: int = 16) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    if hex_color:
        c = QColor(hex_color)
    else:
        c = QColor("#90A4AE")
    p.setBrush(c)
    p.setPen(QColor("#37474F"))
    p.drawRoundedRect(2, 2, size - 4, size - 4, 3, 3)
    p.end()
    return QIcon(pix)


# ---------------------------------------------------------------------------
# main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{__app_name__} - 法律文件標註工具")
        self.resize(1280, 820)

        self.prefs: UserPreferences = UserPreferences.load()
        self.modes: list[AnnotationMode] = list_annotation_modes()
        self.mode: AnnotationMode = self._select_initial_mode()

        self.doc: Optional[Document] = None
        self._active_annotation_id: Optional[str] = None
        self._undo_stack = QUndoStack(self)
        self.act_undo = self._undo_stack.createUndoAction(self, "復原")
        self.act_undo.setShortcut(QKeySequence.Undo)
        self.act_redo = self._undo_stack.createRedoAction(self, "重做")
        self.act_redo.setShortcuts([
            QKeySequence.Redo,
            QKeySequence("Ctrl+Shift+Z"),
        ])

        # central editor + file panel
        self.editor = AnnotationEditor()
        self.editor.set_context_menu_builder(self._build_context_menu)
        self.editor.selection_changed.connect(self._on_selection_changed)
        self.editor.selection_finished.connect(self._show_quick_label_popup)
        self.editor.annotation_clicked.connect(self._on_annotation_clicked)
        self.editor.unannotated_text_clicked.connect(
            self._on_unannotated_text_clicked
        )

        self.file_panel = FilePanel()
        self.file_panel.file_open_requested.connect(self._handle_file_open_requested)

        self.concept_panel = ConceptSidebar() # 新增概念側欄
        self.concept_panel.setVisible(self.mode.id == "term_definition") #根據初始的標註模式，決定是否顯示概念側欄
        self.concept_panel.concept_clicked.connect(self._on_concept_clicked)
        self.concept_panel.add_requested.connect(self._add_concept)
        self.concept_panel.edit_requested.connect(self._edit_concept)
        self.concept_panel.delete_requested.connect(self._delete_concept)

        # 右側垂直切分 (上方檔案、下方概念)
        right_splitter = QSplitter(Qt.Vertical)
        right_splitter.addWidget(self.file_panel)
        right_splitter.addWidget(self.concept_panel)
        right_splitter.setSizes([500, 320])

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.editor)
        splitter.addWidget(right_splitter)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([900, 320])

        self.setCentralWidget(splitter)

        self._build_toolbar()
        self._build_menu()
        self._setup_app_shortcuts()
        self._build_status_bar()

        self._update_actions()
        self._refresh_status()

        # Autosave: 上次寫入的 autosave 檔位置；正式存檔成功後刪除這個檔，
        # 避免 save-as 之後留下指向舊位置的孤兒。
        self._last_autosave_path: Optional[Path] = None
        # 「開啟資料夾…」記住上次選的目錄，僅 session 內有效。
        self._last_browse_dir: Optional[Path] = None
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(_AUTOSAVE_INTERVAL_MS)
        self._autosave_timer.timeout.connect(self._autosave_tick)
        self._autosave_timer.start()

    # ---------------------------------------------------- mode resolution

    def _select_initial_mode(self) -> AnnotationMode:
        if not self.modes:
            QMessageBox.critical(
                self, "錯誤",
                f"找不到任何標註模式設定檔。\n請確認 {cfgmod.annotation_modes_dir()} 內含 *.json。",
            )
            sys.exit(1)
        wanted = self.prefs.active_mode_id
        for m in self.modes:
            if m.id == wanted:
                return m
        return self.modes[0]

    # --------------------------------------------------------------- ui build

    def _build_status_bar(self):
        self.status = QStatusBar(self)
        self.setStatusBar(self.status)
        self.status_mode_label = QLabel()
        self.status.addPermanentWidget(self.status_mode_label)

    def _build_menu(self):
        mb = self.menuBar()

        # File
        m_file = mb.addMenu("檔案(&F)")
        self.act_open = QAction("開啟檔案…", self)
        self.act_open.setShortcut(self.prefs.app_shortcut("open", "Ctrl+O"))
        self.act_open.triggered.connect(self.action_open)
        m_file.addAction(self.act_open)

        self.act_open_folder = QAction("開啟資料夾…", self)
        self.act_open_folder.setShortcut(self.prefs.app_shortcut("open_folder", "Ctrl+Shift+O"))
        self.act_open_folder.triggered.connect(self.action_open_folder)
        m_file.addAction(self.act_open_folder)

        self.act_save = QAction("儲存標註進度 (.lexa)", self)
        self.act_save.setShortcut(self.prefs.app_shortcut("save", "Ctrl+S"))
        self.act_save.triggered.connect(self.action_save)
        m_file.addAction(self.act_save)

        self.act_save_as = QAction("另存標註進度為…", self)
        self.act_save_as.triggered.connect(self.action_save_as)
        m_file.addAction(self.act_save_as)

        m_file.addSeparator()
        self.act_export = QAction("匯出模型用 .txt…", self)
        self.act_export.setShortcut(self.prefs.app_shortcut("export_txt", "Ctrl+E"))
        self.act_export.triggered.connect(self.action_export)
        m_file.addAction(self.act_export)

        m_file.addSeparator()
        self.act_quit = QAction("離開", self)
        self.act_quit.setShortcut("Ctrl+Q")
        self.act_quit.triggered.connect(self.close)
        m_file.addAction(self.act_quit)

        # Edit
        m_edit = mb.addMenu("編輯(&E)")
        m_edit.addAction(self.act_undo)
        m_edit.addAction(self.act_redo)
        self.act_remove_ann = QAction("刪除游標處標註", self)
        self.act_remove_ann.setShortcut(self.prefs.app_shortcut("remove_annotation", "Ctrl+D"))
        self.act_remove_ann.triggered.connect(self.action_remove_annotation_at_cursor)
        m_edit.addAction(self.act_remove_ann)
        # 在主視窗的編輯選單加入全域複製，讓 Ctrl+C 隨時生效
        self.act_copy = QAction("複製選取文字", self)
        self.act_copy.setShortcut("Ctrl+C")
        self.act_copy.triggered.connect(self.editor.copy)
        m_edit.addAction(self.act_copy)

        m_edit.addSeparator()
        self.act_next_file = QAction("下一個檔案", self)
        self.act_next_file.setShortcut(self.prefs.app_shortcut("next_file", "Alt+Down"))
        self.act_next_file.triggered.connect(lambda: self.file_panel.select_relative(1))
        m_edit.addAction(self.act_next_file)

        self.act_prev_file = QAction("上一個檔案", self)
        self.act_prev_file.setShortcut(self.prefs.app_shortcut("prev_file", "Alt+Up"))
        self.act_prev_file.triggered.connect(lambda: self.file_panel.select_relative(-1))
        m_edit.addAction(self.act_prev_file)

        # Annotate
        self.menu_annotate = mb.addMenu("標註(&A)")
        self._populate_annotate_menu()

        # Mode
        m_mode = mb.addMenu("標註模式(&M)")
        self.mode_action_group = QActionGroup(self)
        self.mode_action_group.setExclusive(True)
        for m in self.modes:
            act = QAction(m.name, self, checkable=True)
            act.setData(m.id)
            if m.id == self.mode.id:
                act.setChecked(True)
            act.triggered.connect(self._on_mode_action)
            self.mode_action_group.addAction(act)
            m_mode.addAction(act)
        m_mode.addSeparator()
        act_open_modes_dir = QAction("開啟模式資料夾", self)
        act_open_modes_dir.triggered.connect(self._open_modes_dir)
        m_mode.addAction(act_open_modes_dir)

        # Help
        m_help = mb.addMenu("說明(&H)")
        act_open_logs = QAction("開啟 log 資料夾", self)
        act_open_logs.triggered.connect(self._open_log_dir)
        m_help.addAction(act_open_logs)
        act_copy_diag = QAction("複製診斷資訊", self)
        act_copy_diag.triggered.connect(self._copy_diagnostics)
        m_help.addAction(act_copy_diag)
        m_help.addSeparator()
        act_about = QAction("關於 LexiArbiter", self)
        act_about.triggered.connect(self._show_about)
        m_help.addAction(act_about)

    def _populate_annotate_menu(self):
        self.menu_annotate.clear()
        for g in self.mode.groups:
            sub = self.menu_annotate.addMenu(g.name)
            for lb in g.labels:
                act = QAction(_swatch_icon(lb.color), lb.name, self)
                shortcut = self.prefs.label_shortcut(g.id, lb.id, lb.shortcut)
                if shortcut:
                    act.setShortcut(shortcut)
                act.triggered.connect(self._make_label_handler(g.id, lb.id))
                sub.addAction(act)
            sub.addSeparator()
            act_clear = QAction(f"清除「{g.name}」於選取段", self)
            act_clear.triggered.connect(self._make_clear_group_handler(g.id))
            sub.addAction(act_clear)

    def _build_toolbar(self):
        tb = QToolBar("標註工具列", self)
        tb.setIconSize(QSize(18, 18))
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        self.toolbar = tb

        a_open = tb.addAction(_swatch_icon("#FFFFFF"), "開啟檔案")
        a_open.triggered.connect(self.action_open)

        a_open_folder = tb.addAction(_swatch_icon("#FFFFFF"), "開啟資料夾")
        a_open_folder.triggered.connect(self.action_open_folder)

        a_save = tb.addAction(_swatch_icon("#FFFFFF"), "儲存進度")
        a_save.triggered.connect(self.action_save)

        a_export = tb.addAction(_swatch_icon("#FFFFFF"), "匯出 .txt")
        a_export.triggered.connect(self.action_export)

        tb.addSeparator()
        tb.addAction(self.act_undo)
        tb.addAction(self.act_redo)

        tb.addSeparator()
        self._populate_label_buttons()

    def _populate_label_buttons(self):
        # 移除先前建立的動態按鈕。
        if hasattr(self, "_label_button_actions"):
            for action in self._label_button_actions:
                self.toolbar.removeAction(action)

        self._label_button_actions = []

        show_label_buttons = not self.prefs.behavior.get(
            "auto_popup_on_selection",
            True,
        )

        # 開啟自動 Popup 時，不在工具列重複顯示標籤按鈕。
        if show_label_buttons:
            for group_index, group in enumerate(
                self.mode.groups
            ):
                if group_index > 0:
                    separator = self.toolbar.addSeparator()
                    self._label_button_actions.append(separator)

                group_label = QLabel(f"  {group.name}：")
                action = self.toolbar.addWidget(group_label)
                self._label_button_actions.append(action)

                for label in group.labels:
                    button = QPushButton(label.name)
                    button.setCursor(Qt.PointingHandCursor)

                    shortcut = self.prefs.label_shortcut(
                        group.id,
                        label.id,
                        label.shortcut,
                    )

                    tooltip = label.name
                    if shortcut:
                        tooltip += f"  ({shortcut})"

                    button.setToolTip(tooltip)
                    button.setStyleSheet(
                        self._button_style_for(label)
                    )
                    button.clicked.connect(
                        self._make_label_handler(
                            group.id,
                            label.id,
                        )
                    )

                    action = self.toolbar.addWidget(button)
                    self._label_button_actions.append(action)

        # 以下兩個是一般功能按鈕，不受 Popup 模式影響。
        if self.mode.id == "term_definition":
            separator = self.toolbar.addSeparator()
            self._label_button_actions.append(separator)

            broader_button = QPushButton("設定上位概念")
            broader_button.setCursor(Qt.PointingHandCursor)
            broader_button.setToolTip(
                "設定某個概念所屬的上位概念"
            )
            broader_button.clicked.connect(
                self.set_broader_concept
            )

            action = self.toolbar.addWidget(broader_button)
            self._label_button_actions.append(action)

            explanation_button = QPushButton("編輯概念")
            explanation_button.setCursor(
                Qt.PointingHandCursor
            )
            explanation_button.setToolTip(
                "編輯目前詞語的概念資料與解釋"
            )
            explanation_button.clicked.connect(
                self.apply_explanation
            )

            action = self.toolbar.addWidget(
                explanation_button
            )
            self._label_button_actions.append(action)

    def _button_style_for(self, lb: LabelDef) -> str:
        if lb.color:
            return (
                f"QPushButton {{ background-color: {lb.color}; color: #1B1B1B; "
                f"border: 1px solid #333; border-radius: 6px; padding: 4px 12px; }}"
                f"QPushButton:hover {{ border: 1px solid #000; }}"
            )
        return (
            "QPushButton { background-color: #ECEFF1; color: #1B1B1B; "
            "border: 1px solid #90A4AE; border-radius: 6px; padding: 4px 12px; }"
            "QPushButton:hover { border: 1px solid #455A64; }"
        )

    # ----------------------------------------------------------- shortcuts

    def _setup_app_shortcuts(self):
        # menu actions already carry shortcuts; nothing additional here.
        pass

    # --------------------------------------------------------- annotate ops

    def _make_label_handler(self, group_id: str, label_id: str):
        def handler(*_):
            self.apply_label(group_id, label_id)
        return handler

    def _make_clear_group_handler(self, group_id: str):
        def handler(*_):
            self.clear_group_in_selection(group_id)
        return handler

    def _confirm_replace_existing_group_label(
        self, ann: Annotation, group_id: str, new_label_id: str
    ) -> bool:
        """若 ann 在 group_id 已有不同 label，跳出確認對話框。
        回傳 True 表示可繼續覆寫；False 表示使用者取消。
        純新增、重複套用同 label 一律放行不打擾。
        """
        existing = ann.labels.get(group_id)
        if existing is None or existing == new_label_id:
            return True
        group = next((g for g in self.mode.groups if g.id == group_id), None)
        if group is None:
            return True
        old_name = next((l.name for l in group.labels if l.id == existing), existing)
        new_name = next((l.name for l in group.labels if l.id == new_label_id), new_label_id)
        reply = QMessageBox.question(
            self, "重疊處理",
            f"此範圍在「{group.name}」群組已標為「{old_name}」，\n"
            f"要改為「{new_name}」嗎？",
            QMessageBox.Yes | QMessageBox.Cancel,
        )
        return reply == QMessageBox.Yes

    def _resolve_same_group_overlap(
        self, s: int, e: int, group_id: str,
        exclude_id: Optional[str] = None,
    ) -> bool:
        """檢查 [s, e) 範圍內除了 exclude_id 以外，是否有其他標註已佔用 group_id。

        有重疊就彈窗詢問是否刪除既有的同群組 label：
        - 使用者同意 → 就地清掉（若清空整條 ann 的 labels 則整條移除），回 True。
        - 使用者取消 → 回 False，呼叫端應 return 不寫入。
        無重疊直接回 True，呼叫端繼續即可。

        三條 apply_label 分支共用同一邏輯，避免「exact match / cursor 內套用」
        路徑繞過跨標註同群組衝突檢查（會在匯出時切碎成奇怪段落）。
        """
        if self.doc is None:
            return False
        overlap = [
            a for a in self.doc.annotations_in_range_for_group(s, e, group_id)
            if a.id != exclude_id
        ]
        if not overlap:
            return True
        reply = QMessageBox.question(
            self, "重疊處理",
            "套用範圍與既有同群組標註重疊。\n要刪除既有標註的該群組 label 再套用嗎？",
            QMessageBox.Yes | QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return False
        for a in overlap:
            a.labels.pop(group_id, None)
            if not a.labels:
                self.doc.remove_annotation(a.id)
        return True


    # 搜尋文件中所有與人工選取術語完全相同的文字。
    # 如果某個相同長詞範圍內只包含既有的較短術語，會以長詞取代短詞；
    # 交叉重疊或相同範圍等不安全情況仍會略過。
    def _auto_annotate_same_terms(
        self,
        source_ann: Annotation,
        group_id: str,
        label_id: str,
    ) -> tuple[int, int, int]:
        """自動標註文件中所有與人工標註完全相同的術語。

        回傳：
        added    = 新增的標註數量
        replaced = 被長詞取代的短詞標註數量
        skipped  = 因不安全重疊而略過的數量
        """
        if self.doc is None:
            return 0, 0, 0

        term = self.doc.text[source_ann.start:source_ann.end]

        if not term or not term.strip():
            return 0, 0, 0

        added = 0
        replaced = 0
        skipped = 0
        search_from = 0

        while True:
            match_start = self.doc.text.find(term, search_from)

            if match_start == -1:
                break

            match_end = match_start + len(term)
            search_from = match_end

            if (
                match_start == source_ann.start
                and match_end == source_ann.end
            ):
                continue

            overlaps = self.doc.annotations_in_range_for_group(
                match_start,
                match_end,
                group_id,
            )

            if overlaps:
                # 只有「既有標註被新長詞完整包住，而且不是相同範圍」時，
                # 才能安全地以長詞取代。交叉重疊一律保留並略過。
                can_replace_nested = all(
                    match_start <= ann.start
                    and ann.end <= match_end
                    and (
                        ann.start != match_start
                        or ann.end != match_end
                    )
                    for ann in overlaps
                )

                if not can_replace_nested:
                    skipped += 1
                    continue

                for ann in overlaps:
                    ann.labels.pop(group_id, None)
                    if not ann.labels:
                        self.doc.remove_annotation(ann.id)
                    replaced += 1

            auto_ann = Annotation(
                start=match_start,
                end=match_end,
                labels={group_id: label_id},
                concept_id=source_ann.concept_id,
                explanation="",
                broader_concept_id=source_ann.broader_concept_id,
            )

            self.doc.add_annotation(auto_ann)
            added += 1

        return added, replaced, skipped

    

    # 為目前選取的單筆術語手動輸入解釋。
    def apply_explanation(self):
        if self.doc is None:
            self.status.showMessage(
                "請先開啟一個檔案。",
                4000,
            )
            return

        if self.mode.id != "term_definition":
            self.status.showMessage(
                "只有「術語與解釋」模式可以標記解釋。",
                4000,
            )
            return

        # 必須先單擊一筆已標註的術語。
        if self._active_annotation_id is None:
            QMessageBox.information(
                self,
                "尚未選擇術語",
                "請先單擊一筆黃色術語標註，再輸入它的解釋。",
            )
            return

        ann = self.doc.find_annotation(
            self._active_annotation_id
        )

        # 避免目前記住的是已經被刪除的標註。
        if ann is None:
            self._active_annotation_id = None
            QMessageBox.warning(
                self,
                "找不到術語",
                "先前選擇的術語標註已不存在，"
                "請重新單擊一筆術語。",
            )
            return

        if ann.concept_id is None:
            QMessageBox.warning(
                self,
                "術語沒有概念",
                "這筆術語尚未連結到任何概念，"
                "請重新建立術語標註。",
            )
            return

        # Reuse the concept editor from the right sidebar for the clicked term.
        self._edit_concept(ann.concept_id)

    # 設定某個概念所屬的上位概念，
    # 並同步更新所有使用該概念的術語標註。
    def _concept_broader_map(self) -> dict[str, str]:
        """從術語標註整理 concept -> broader concept 的階層關係。"""
        if self.doc is None:
            return {}
        result: dict[str, str] = dict(self.doc.broader_concepts)
        for ann in self.doc.annotations:
            if (
                ann.concept_id in self.doc.concepts
                and ann.broader_concept_id in self.doc.concepts
            ):
                result.setdefault(
                    ann.concept_id,
                    ann.broader_concept_id,
                )
        return result

    def _mark_concepts_changed(
        self,
        before: Optional[Document] = None,
        history_label: str = "編輯概念",
    ):
        if self.doc is None:
            return
        self.doc.dirty = True
        self._record_undo(before, history_label)
        self.concept_panel.refresh(self.doc)
        self.editor.refresh_highlights()
        self._refresh_status()
        self._update_window_title()

    def _add_concept(self):
        if self.doc is None:
            self.status.showMessage("請先開啟一個文件。", 4000)
            return
        concept_id = "C_" + uuid.uuid4().hex[:8]
        dialog = ConceptEditDialog(
            concept_id,
            "",
            self.doc.concepts,
            self._concept_broader_map(),
            self.doc.annotations,
            self.doc.text,
            parent=self,
        )
        dialog.setWindowTitle("新增文件概念")
        if not self._exec_concept_edit_dialog(dialog):
            return
        name, parent_id, _ann_id, _explanation = dialog.result_values()
        if not name:
            QMessageBox.information(self, "概念名稱不可空白", "請輸入概念名稱。")
            return
        if name in self.doc.concepts.values():
            QMessageBox.information(self, "概念已存在", "請使用既有概念，或輸入不同名稱。")
            return
        before = self._snapshot_document()
        self.doc.concepts[concept_id] = name
        self.doc.broader_concepts[concept_id] = parent_id or concept_id
        self._mark_concepts_changed(before, "新增概念")

    def _edit_concept(self, concept_id: str):
        if self.doc is None or concept_id not in self.doc.concepts:
            return
        dialog = ConceptEditDialog(
            concept_id,
            self.doc.concepts[concept_id],
            self.doc.concepts,
            self._concept_broader_map(),
            self.doc.annotations,
            self.doc.text,
            self._active_annotation_id,
            self,
        )
        occurrences_selected = getattr(dialog, "occurrences_selected", None)
        if occurrences_selected is not None:
            occurrences_selected.connect(self.editor.highlight_annotations)
            initial_occurrence_ids = dialog.selected_annotation_ids()
            if initial_occurrence_ids:
                self.editor.highlight_annotations(initial_occurrence_ids)
        if not self._exec_concept_edit_dialog(dialog):
            return
        result = dialog.result_values()
        name, parent_id, selected_annotation_ids, explanation = result
        if isinstance(selected_annotation_ids, str):
            selected_annotation_ids = [selected_annotation_ids]
        selected_annotation_ids = selected_annotation_ids or []
        if not name:
            QMessageBox.information(self, "概念名稱不可空白", "請輸入概念名稱。")
            return
        if any(cid != concept_id and value == name for cid, value in self.doc.concepts.items()):
            QMessageBox.information(self, "概念已存在", "已有相同名稱的概念。")
            return
        before = self._snapshot_document()
        self.doc.concepts[concept_id] = name
        self.doc.broader_concepts[concept_id] = parent_id or concept_id
        for ann in self.doc.annotations:
            if ann.concept_id == concept_id:
                ann.broader_concept_id = parent_id or concept_id
        if explanation is not None:
            for ann_id in selected_annotation_ids:
                ann = self.doc.find_annotation(ann_id)
                if ann and ann.concept_id == concept_id:
                    ann.explanation = explanation
        self._mark_concepts_changed(before, "編輯概念")

    def _exec_concept_edit_dialog(
        self,
        dialog: ConceptEditDialog,
    ) -> bool:
        result = dialog.exec()
        while result == dialog.PICK_PARENT_FROM_TEXT:
            self._pick_parent_concept_from_text(dialog)
            result = dialog.exec()
        return bool(result)

    def _pick_parent_concept_from_text(
        self,
        dialog: ConceptEditDialog,
    ) -> Optional[str]:
        if self.doc is None:
            return None

        loop = QEventLoop(self)
        selected: dict[str, str] = {}
        self.status.showMessage(
            "請點選文件中已標註的概念；按 Esc 取消。",
            0,
        )

        def on_annotation_clicked(ann_id: str):
            ann = self.doc.find_annotation(ann_id) if self.doc else None
            if (
                ann is None
                or ann.concept_id is None
                or ann.concept_id not in self.doc.concepts
            ):
                self.status.showMessage(
                    "這筆標註尚未連結概念，請改點其他已標註的文字。",
                    4000,
                )
                return
            if not dialog.set_parent_concept(ann.concept_id):
                self.status.showMessage(
                    "不能選擇目前概念或其下位概念作為上位概念。",
                    4000,
                )
                return

            selected["concept_id"] = ann.concept_id
            self.status.showMessage(
                f"已選擇「{self.doc.concepts[ann.concept_id]}」作為上位概念。",
                4000,
            )
            loop.quit()

        self.editor.annotation_clicked.connect(on_annotation_clicked)
        cancel_shortcut = QShortcut(QKeySequence("Esc"), self)
        cancel_shortcut.setContext(Qt.ApplicationShortcut)
        cancel_shortcut.activated.connect(loop.quit)
        try:
            loop.exec()
        finally:
            self.editor.annotation_clicked.disconnect(on_annotation_clicked)
            cancel_shortcut.deleteLater()
            self._refresh_status()

        return selected.get("concept_id")

    def _delete_concept(self, concept_id: str):
        if self.doc is None or concept_id not in self.doc.concepts:
            return
        name = self.doc.concepts[concept_id]
        if QMessageBox.question(
            self,
            "刪除文件概念",
            f"確定刪除「{name}」？其術語標註會解除概念連結，子概念會移到原上位概念。",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        ) != QMessageBox.Yes:
            return
        before = self._snapshot_document()

        self._remove_concept_record(concept_id)
        self._mark_concepts_changed(before, "刪除概念")

    def _remove_concept_record(self, concept_id: str) -> bool:
        """Delete a concept and unlink its annotations without prompting."""
        if self.doc is None or concept_id not in self.doc.concepts:
            return False

        parent_id = self._concept_broader_map().get(concept_id, concept_id)
        if parent_id == concept_id:
            parent_id = concept_id
        for child_id, broader_id in list(self._concept_broader_map().items()):
            if child_id != concept_id and broader_id == concept_id:
                new_parent = parent_id if parent_id != concept_id else child_id
                self.doc.broader_concepts[child_id] = new_parent
                for ann in self.doc.annotations:
                    if ann.concept_id == child_id:
                        ann.broader_concept_id = new_parent
        for ann in self.doc.annotations:
            if ann.concept_id == concept_id:
                ann.concept_id = None
                ann.broader_concept_id = None
            elif ann.broader_concept_id == concept_id:
                ann.broader_concept_id = parent_id if parent_id != concept_id else ann.concept_id
        self.doc.concepts.pop(concept_id, None)
        self.doc.broader_concepts.pop(concept_id, None)
        return True

    def set_broader_concept(self):
        if self.doc is None:
            self.status.showMessage(
                "請先開啟一個檔案。",
                4000,
            )
            return

        if self.mode.id != "term_definition":
            self.status.showMessage(
                "只有「術語與解釋」模式可以設定上位概念。",
                4000,
            )
            return

        if not self.doc.concepts:
            QMessageBox.information(
                self,
                "尚無概念",
                "請先標記至少一個專業術語，"
                "再設定上位概念。",
            )
            return

        # 第一次選擇：要被設定的子概念。
        target_dialog = ConceptSelectionDialog(
            self.doc.concepts,
            default_text="",
            parent=self,
            allow_new=False,
            broader_by_concept=self._concept_broader_map(),
        )
        target_dialog.setWindowTitle(
            "步驟 1：選擇要設定的既有概念"
        )

        if not target_dialog.exec():
            return

        target_id, target_new_name = (
            target_dialog.get_result()
        )

        if target_id is None or target_new_name:
            QMessageBox.information(
                self,
                "請選擇既有概念",
                "請從上方清單選擇要設定的概念。",
            )
            return
        target_name = self.doc.concepts.get(target_id, target_id)

        # 所有概念都可以成為上位概念。
        # 最上層概念依作業格式，以自己作為 broader concept。
        broader_candidates = dict(
            self.doc.concepts
        )

        # 第二次選擇：上位概念。
        broader_dialog = ConceptSelectionDialog(
            broader_candidates,
            default_text="",
            parent=self,
            broader_by_concept=self._concept_broader_map(),
        )
        broader_dialog.setWindowTitle(
            f"步驟 2：選擇「{target_name}」的上位概念"
        )
        broader_dialog.input_label.setText(
            f"2. 或直接輸入「{target_name}」的上位概念："
        )
        broader_dialog.input_edit.setPlaceholderText(
            f"輸入「{target_name}」的上位概念名稱..."
        )

        if not broader_dialog.exec():
            return

        broader_id, broader_new_name = (
            broader_dialog.get_result()
        )

        before = self._snapshot_document()
        if broader_new_name:
            broader_id = "C_" + uuid.uuid4().hex[:8]
            self.doc.concepts[broader_id] = broader_new_name
            self.concept_panel.refresh(self.doc)
        elif broader_id is None:
            QMessageBox.information(
                self,
                "請選擇既有概念",
                "請從上方清單選擇上位概念，或輸入新的上位概念。",
            )
            return

        # 概念階層只允許無循環的 parent 關係；選自己代表設為根概念。
        if broader_id != target_id:
            parent_map = self._concept_broader_map()
            ancestor = broader_id
            seen = {target_id}
            while ancestor in parent_map and parent_map[ancestor] != ancestor:
                if ancestor in seen:
                    QMessageBox.warning(self, "概念階層循環", "此設定會形成循環，請選擇其他上位概念。")
                    self._restore_document(before)
                    return
                seen.add(ancestor)
                ancestor = parent_map[ancestor]
            if ancestor in seen:
                QMessageBox.warning(self, "概念階層循環", "此設定會形成循環，請選擇其他上位概念。")
                self._restore_document(before)
                return

        # 找出所有使用子概念的術語標註。
        linked_annotations = [
            ann
            for ann in self.doc.annotations
            if ann.concept_id == target_id
        ]

        # 將所有相同概念的術語設成同一個上位概念。
        self.doc.broader_concepts[target_id] = broader_id
        for ann in linked_annotations:
            ann.broader_concept_id = broader_id

        self.doc.dirty = True
        self._record_undo(before, "設定上位概念")
        self.concept_panel.refresh(self.doc)
        self._refresh_status()
        self._update_window_title()

        target_name = self.doc.concepts.get(
            target_id,
            target_id,
        )
        broader_name = self.doc.concepts.get(
            broader_id,
            broader_id,
        )

        self.status.showMessage(
            f"已將「{target_name}」的上位概念"
            f"設定為「{broader_name}」，"
            f"共更新 {len(linked_annotations)} 個術語標註。",
            7000,
        )

    def apply_label(self, group_id: str, label_id: str):
        if self.doc is None:
            self.status.showMessage("請先開啟一個檔案再進行標註。", 4000)
            return

        before = self._snapshot_document()
        concept_id = None
        concept_new_name = None
        # 如果使用者設定開啟 Popup，檢查是否為 term_definition 模式、是否開啟彈窗、以及是否有選取文字，則跳出概念選擇器
        if self.mode.id == "term_definition" and self.prefs.behavior.get("show_concept_popup", True) and self.editor.has_selection():
            
            # 使用 storage offset 取得原始文件文字，
            # 避免 Qt 顯示文字與原始換行座標不同。
            s, e = self.editor.storage_selection()
            selected_text = self.doc.text[s:e].strip()
                
            # 將選取的文字作為 default_text 傳入對話框
            dlg = ConceptSelectionDialog(
                self.doc.concepts,
                default_text=selected_text,
                parent=self,
                broader_by_concept=self._concept_broader_map(),
            )
            if dlg.exec():
                selected_id, new_name = dlg.get_result()
                if new_name:
                    # Defer adding the concept until the label operation succeeds.
                    concept_id = "C_" + uuid.uuid4().hex[:8]
                    concept_new_name = new_name
                else:
                    concept_id = selected_id
            else:
                return # 使用者按取消，放棄標註
       
        try:
            if not self.editor.has_selection():
                ann_id = self.editor.annotation_at_cursor()
                if ann_id is None:
                    self.status.showMessage("請先選取一段文字，或將游標放在已標註的範圍內。", 4000)
                    return
                ann = self.doc.find_annotation(ann_id)
                if ann is None:
                    return
                if not self._confirm_replace_existing_group_label(ann, group_id, label_id):
                    return
                if not self._resolve_same_group_overlap(
                    ann.start, ann.end, group_id, exclude_id=ann.id,
                ):
                    return
                if concept_new_name:
                    self.doc.concepts[concept_id] = concept_new_name
                    self.doc.broader_concepts[concept_id] = concept_id
                ann.labels[group_id] = label_id
                if concept_id: ann.concept_id = concept_id
                self.doc.dirty = True
                self.editor.refresh_highlights()
                self._refresh_status()
                self._update_window_title()
                self._record_undo(before, "套用標籤")
                return

            s, e = self.editor.storage_selection()
            # If selection coincides exactly with an existing annotation, update it.
            existing = [a for a in self.doc.annotations
                        if a.start == s and a.end == e]
            if existing:
                ann = existing[0]
                if not self._confirm_replace_existing_group_label(ann, group_id, label_id):
                    return
                if not self._resolve_same_group_overlap(
                    s, e, group_id, exclude_id=ann.id,
                ):
                    return
                if concept_new_name:
                    self.doc.concepts[concept_id] = concept_new_name
                    self.doc.broader_concepts[concept_id] = concept_id
                ann.labels[group_id] = label_id
                if concept_id: ann.concept_id = concept_id
            else:
                # 跨群組重疊應允許並存（每個群組是獨立任務）；只在「同群組」
                # 重疊時才提示，因為同群組 label 互斥。
                if not self._resolve_same_group_overlap(s, e, group_id):
                    return
                if concept_new_name:
                    self.doc.concepts[concept_id] = concept_new_name
                    self.doc.broader_concepts[concept_id] = concept_id
                ann = Annotation(start=s, end=e, labels={group_id: label_id}, concept_id=concept_id)
                self.doc.add_annotation(ann)

            # 只有在「術語與解釋」模式下標記「專業術語」時，
            # 才自動搜尋並標註文件中的相同術語。
            auto_added = 0
            auto_replaced = 0
            auto_skipped = 0

            if (
                self.mode.id == "term_definition"
                and group_id == "entity"
                and label_id == "term"
            ):
                (
                    auto_added,
                    auto_replaced,
                    auto_skipped,
                ) = self._auto_annotate_same_terms(
                    ann,
                    group_id,
                    label_id,
                )

            self.doc.dirty = True
            self.editor.refresh_highlights()
            # 套用專業術語標籤時可能剛建立概念；同步重建右側概念樹，
            # 並展開、定位到剛套用的概念，避免新概念藏在收合的上位項目下。
            if self.mode.id == "term_definition":
                self.concept_panel.refresh(self.doc)
                if ann.concept_id in self.doc.concepts:
                    self.concept_panel.select_concept(ann.concept_id)
            self._refresh_status()
            self._update_window_title()
            self._record_undo(before, "套用標籤")

            # 將自動標註結果顯示在視窗下方的狀態列。
            if (
                auto_added > 0
                or auto_replaced > 0
                or auto_skipped > 0
            ):
                self.status.showMessage(
                    f"已自動新增 {auto_added} 個相同術語；"
                    f"以長詞取代 {auto_replaced} 個短詞標註；"
                    f"因重疊略過 {auto_skipped} 個。",
                    6000,
                )
        except Exception:
            log.error("apply_label 發生例外 group=%s label=%s", group_id, label_id, exc_info=True)
            QMessageBox.critical(self, "標註錯誤", "套用標籤時發生錯誤，請查看 logs/lexiarbiter.log。")

    def clear_group_in_selection(self, group_id: str):
        if self.doc is None:
            return
        before = self._snapshot_document()
        s, e = self.editor.storage_selection()
        if e <= s:
            ann_id = self.editor.annotation_at_cursor()
            if ann_id is None:
                return
            ann = self.doc.find_annotation(ann_id)
            if ann is None:
                return
            ann.labels.pop(group_id, None)
            if not ann.labels:
                self.doc.remove_annotation(ann.id)
        else:
            for a in list(self.doc.annotations_in_range(s, e)):
                a.labels.pop(group_id, None)
                if not a.labels:
                    self.doc.remove_annotation(a.id)
        self.doc.dirty = True
        self._record_undo(before, "清除標註")
        self.editor.refresh_highlights()
        self._refresh_status()
        self._update_window_title()

    def action_remove_annotation_at_cursor(self):
        if self.doc is None:
            return

        before = self._snapshot_document()
        removed_ids: set[str] = set()
        affected_concept_ids: set[str] = set()

        # 有選取範圍時，刪除範圍內的標註。
        s, e = self.editor.storage_selection()

        if e > s:
            for ann in list(self.doc.annotations_in_range(s, e)):
                if ann.concept_id:
                    affected_concept_ids.add(ann.concept_id)
                removed_ids.add(ann.id)
                self.doc.remove_annotation(ann.id)

        else:
            # 沒有選取範圍時，刪除游標位置的標註。
            ann_id = self.editor.annotation_at_cursor()

            if ann_id is None:
                self.status.showMessage(
                    "游標處沒有標註可刪除。",
                    3000,
                )
                return

            ann = self.doc.find_annotation(ann_id)
            if ann and ann.concept_id:
                affected_concept_ids.add(ann.concept_id)
            if self.doc.remove_annotation(ann_id):
                removed_ids.add(ann_id)

        concepts_removed = False
        if affected_concept_ids:
            broader_map = self._concept_broader_map()

            def concept_depth(concept_id: str) -> int:
                depth = 0
                current = broader_map.get(concept_id, concept_id)
                visited = {concept_id}
                while current != concept_id and current not in visited:
                    depth += 1
                    visited.add(current)
                    current = broader_map.get(current, current)
                return depth

            # Remove descendants first so nested concepts can be reparented
            # before their own ancestor disappears.
            for concept_id in sorted(
                affected_concept_ids,
                key=concept_depth,
                reverse=True,
            ):
                concepts_removed |= self._remove_concept_record(concept_id)

        if removed_ids:
            # Remove annotation colors, concept-wide highlight, and the text
            # selection together so no stale marker remains on the document.
            self.editor.clear_annotation_highlights()
        if self._active_annotation_id in removed_ids:
            self._active_annotation_id = None

        if concepts_removed:
            self.concept_panel.refresh(self.doc)

        history_label = "刪除標註與概念" if concepts_removed else "刪除標註"
        self._record_undo(before, history_label)
        self.editor.refresh_highlights()
        self._refresh_status()
        self._update_window_title()

    # ----------------------------------------------------------- file ops

    def _confirm_unsaved(self) -> bool:
        if self.doc is None or not self.doc.dirty:
            return True
        if not self.prefs.behavior.get("confirm_unsaved_on_switch", True):
            return True
        reply = QMessageBox.question(
            self, "未儲存",
            "目前的標註尚未儲存，是否要儲存？",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
        )
        if reply == QMessageBox.Save:
            return self.action_save()
        if reply == QMessageBox.Discard:
            # 使用者主動捨棄 → autosave 也視為過期，避免下次開檔被誤報為「較新存檔」。
            self._cleanup_autosave()
            return True
        return False

    def action_open(self):
        if not self._confirm_unsaved():
            return
        start_dir = ""
        if self.doc and self.doc.file_path:
            start_dir = str(Path(self.doc.file_path).parent)
        path, _ = QFileDialog.getOpenFileName(
            self, "開啟檔案", start_dir,
            "支援的格式 (*.json *.lexa *.txt *.docx);;判決 JSON (*.json);;"
            "Word 檔 (*.docx);;標註進度 (*.lexa);;模型匯出 (*.txt);;所有檔案 (*.*)"
        )
        if path:
            self.load_file(path)

    def action_open_folder(self):
        """瀏覽任意資料夾並把該資料夾的 .json/.lexa/.txt 列到右側標籤頁。

        不會關閉目前已開啟的文件；只是切換右側清單檢視。
        """
        if self._last_browse_dir is not None:
            start_dir = str(self._last_browse_dir)
        elif self.doc and self.doc.file_path:
            start_dir = str(Path(self.doc.file_path).parent)
        else:
            start_dir = ""
        folder = QFileDialog.getExistingDirectory(self, "開啟資料夾", start_dir)
        if not folder:
            return
        p = Path(folder)
        self._last_browse_dir = p
        current = (Path(self.doc.file_path)
                   if (self.doc and self.doc.file_path) else None)
        self.file_panel.set_directory(p, current)
        self.status.showMessage(f"已切換資料夾：{p}", 4000)

    def _handle_file_open_requested(self, path: str):
        if self.doc and self.doc.file_path and Path(path).resolve() == Path(self.doc.file_path).resolve():
            return
        if not self._confirm_unsaved():
            return
        self.load_file(path)

    def load_file(self, path: str):
        log.info("開啟檔案：%s", path)
        src_path = Path(path)

        # 偵測同位置較新的 autosave 檔，詢問使用者是否還原。
        autosave = self._autosave_companion(src_path)
        use_autosave = False
        if autosave is not None:
            reply = QMessageBox.question(
                self, "偵測到自動存檔",
                f"找到比 {src_path.name} 更新的自動存檔：\n{autosave.name}\n\n"
                "要載入自動存檔（保留未存的標註）嗎？\n"
                "選「否」會直接開啟原檔案。",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                use_autosave = True
                log.info("使用者選擇從 autosave 還原：%s", autosave)

        suffix = src_path.suffix.lower()
        txt_summary: Optional[dict] = None
        try:
            if use_autosave:
                doc = iomod.load_lexa(autosave)
                # 視為「使用者開啟原檔」：保留原 file_path（如果是 .lexa 直接覆蓋；
                # 如果是 .json 或 .txt 則 file_path=None，下次按存檔會走 save_as）。
                # dirty=True 提醒使用者這份內容尚未正式存檔。
                doc.file_path = (str(src_path)
                                 if suffix == ".lexa"
                                 else None)
                doc.dirty = True
            elif suffix == ".txt":
                # .txt 需要 schema 才能 resolve tag 字串，不走 load_any。
                doc, txt_summary = iomod.parse_legacy_txt(path, self.mode)
            else:
                doc = iomod.load_any(path)
        except Exception as e:
            log.error("讀檔失敗：%s", path, exc_info=True)
            QMessageBox.critical(self, "讀檔失敗", str(e))
            return

        # If the doc was saved under a different schema, warn.
        if doc.schema_id and doc.schema_id != self.mode.id:
            target = next((m for m in self.modes if m.id == doc.schema_id), None)
            if target is not None:
                reply = QMessageBox.question(
                    self, "切換標註模式？",
                    f"此檔案是以「{target.name}」模式標註的，是否切換到該模式以正確顯示？",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply == QMessageBox.Yes:
                    self._switch_mode(target.id, persist=False)

        # .txt 載入：若有未知標籤，要 user 確認是否繼續（預設 Cancel）。
        if txt_summary is not None and txt_summary["unknown_tags"]:
            lines = [f'  · "{t}" × {n}'
                     for t, n in txt_summary["unknown_tags"].items()]
            reply = QMessageBox.question(
                self, "發現未知標籤",
                f"此 .txt 包含 {len(txt_summary['unknown_tags'])} 個目前模式不認得的標籤：\n"
                + "\n".join(lines)
                + "\n\n要繼續開啟嗎？這些段落仍會匯入，"
                "但未知標籤會記到 note 欄位、不會出現在 label 中。",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if reply != QMessageBox.Yes:
                log.info("使用者於未知標籤確認時取消載入：%s", path)
                return

        self._undo_stack.clear()
        self.doc = doc
        self._active_annotation_id = None
        self.editor.attach(doc, self.mode, self.prefs)
        self.concept_panel.refresh(doc) # 刷新概念側欄
        self._update_window_title()
        # Update file panel to the directory of this doc.
        self.file_panel.set_directory(Path(path).parent, Path(path))
        self._update_actions()
        self._refresh_status()

        # 偵測同群組跨標註衝突（舊版 apply_label bug 殘留 / 手改 .lexa 可能造成）。
        # 不自動修改使用者資料，只提示位置以便手動清理；匯出時這些段會被切碎。
        self._warn_if_group_conflicts(doc)

        # .txt 載入後彙整顯示偵測結果（反向驗證匯出 bug 的重要線索）。
        if txt_summary is not None:
            notable = self._format_txt_summary(txt_summary)
            if notable:
                QMessageBox.information(self, "已匯入 .txt", notable)

    def _warn_if_group_conflicts(self, doc: Document) -> None:
        """掃描 doc 內同群組衝突段，若有則彈 warning 列前幾筆位置。"""
        conflicts = detect_same_group_conflicts(doc.annotations)
        if not conflicts:
            return
        lines: list[str] = []
        for s, e, gid, lids in conflicts[:8]:
            grp = self.mode.group(gid)
            gname = grp.name if grp else gid
            lnames = []
            for lid in lids:
                lb = grp.label(lid) if grp else None
                lnames.append(lb.name if lb else lid)
            preview = doc.text[s:min(s + 15, e)].replace("\r", " ").replace("\n", " ")
            lines.append(
                f"  · 段 {s}-{e}「{preview}…」群組「{gname}」：{' / '.join(lnames)}"
            )
        msg = (
            f"偵測到 {len(conflicts)} 個同群組衝突段落（同一字元範圍內、同群組出現多個 label）。\n"
            "匯出 .txt 時這些段會被切碎成非預期的小段，建議用「標註選單 → 清除『群組』於選取段」"
            "清掉重複的範圍後重新標註。\n\n"
            + "\n".join(lines)
        )
        if len(conflicts) > 8:
            msg += f"\n（其餘 {len(conflicts) - 8} 筆已略過）"
        QMessageBox.warning(self, "標註資料偵測到衝突", msg)

    def action_save(self) -> bool:
        if self.doc is None:
            return False
        path = self.doc.file_path
        if path and path.lower().endswith(".lexa"):
            log.info("儲存檔案：%s", path)
            try:
                iomod.save_lexa(self.doc, path, self.mode)
            except Exception as e:
                log.error("儲存失敗：%s", path, exc_info=True)
                QMessageBox.critical(self, "儲存失敗", str(e))
                return False
            self._cleanup_autosave()
            self.status.showMessage(f"已儲存：{path}", 4000)
            self.file_panel.refresh()
            self._update_window_title()
            return True
        return self.action_save_as()

    def action_save_as(self) -> bool:
        if self.doc is None:
            return False
        if self.doc.file_path:
            base = Path(self.doc.file_path)
            default = str(base.with_suffix(".lexa"))
        else:
            default = "annotation.lexa"
        path, _ = QFileDialog.getSaveFileName(
            self, "另存標註進度", default,
            "LexiArbiter 進度 (*.lexa)"
        )
        if not path:
            return False
        if not path.lower().endswith(".lexa"):
            path = path + ".lexa"
        log.info("另存檔案：%s", path)
        try:
            iomod.save_lexa(self.doc, path, self.mode)
        except Exception as e:
            log.error("另存失敗：%s", path, exc_info=True)
            QMessageBox.critical(self, "儲存失敗", str(e))
            return False
        self._cleanup_autosave()
        self.status.showMessage(f"已儲存：{path}", 4000)
        self.file_panel.set_directory(Path(path).parent, Path(path))
        self._update_window_title()
        return True

    def action_export(self):
        if self.doc is None:
            return
        if not self.doc.annotations:
            QMessageBox.information(self, "提示", "目前沒有任何標註可以匯出。")
            return
        if self.doc.file_path:
            base = Path(self.doc.file_path)
            if base.suffix.lower() == ".txt":
                # 從 .txt 載入時：只給目錄、不給檔名，強制 user 重新命名，
                # 避免不小心覆蓋原檔（原檔可能用於 byte-identical 反向比對）。
                default = str(base.parent) + os.sep
            else:
                default = str(base.with_suffix(".txt"))
        else:
            default = "export.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "匯出模型用 .txt", default,
            "純文字 (*.txt)"
        )
        if not path:
            return
        if not path.lower().endswith(".txt"):
            path = path + ".txt"
        log.info("匯出 .txt：%s", path)
        try:
            summary = iomod.export_txt(self.doc, path, self.mode)
        except Exception as e:
            log.error("匯出失敗：%s", path, exc_info=True)
            QMessageBox.critical(self, "匯出失敗", str(e))
            return

        msg = (
            f"成功匯出 {summary['written']} 段。\n"
            f"未涵蓋字元：{summary['unannotated_chars']}\n"
            f"群組未填齊段落：{summary['partial_count']}"
        )
        if summary["warnings"]:
            msg += "\n\n警告：\n" + "\n".join(summary["warnings"][:8])
            if len(summary["warnings"]) > 8:
                msg += f"\n（其餘 {len(summary['warnings']) - 8} 條已略過）"

        if (summary["partial_count"] > 0
                and self.prefs.behavior.get("warn_partial_groups_on_export", True)):
            QMessageBox.warning(self, "匯出完成（含警告）", msg)
        else:
            QMessageBox.information(self, "匯出完成", msg)
        self.file_panel.refresh()

    @staticmethod
    def _format_txt_summary(summary: dict) -> str:
        """組裝 .txt 載入後的彙整訊息；無任何異常則回空字串（不彈窗）。"""
        nl_label = "CRLF" if summary["dominant_newline"] == "\r\n" else "LF"
        head = f"已匯入 {summary['paragraphs']} 段（主導換行：{nl_label}）。"

        details: list[str] = []
        if summary["unknown_tags"]:
            details.append(f"未知標籤：{sum(summary['unknown_tags'].values())} 次")
        if summary["non_export_group_tags"]:
            details.append(
                f"非匯出群組標籤：{sum(summary['non_export_group_tags'].values())} 次"
                "（re-export 會被丟棄）"
            )
        if summary["empty_tag_paragraphs"]:
            details.append(f"無標籤的 <P> 段：{summary['empty_tag_paragraphs']}")
        if summary["duplicate_tags_paragraphs"]:
            details.append(
                f"重複標籤的 <P> 段：{summary['duplicate_tags_paragraphs']}"
            )
        if summary["group_collisions_paragraphs"]:
            details.append(
                f"同群組多標籤的 <P> 段：{summary['group_collisions_paragraphs']}"
                "（採後標註的版本）"
            )
        if summary["stray_text_chars"]:
            details.append(
                f"<P> 區塊外夾雜非空白字元：{summary['stray_text_chars']}"
            )

        if not details:
            return ""
        return head + "\n\n偵測到以下狀況（可能是匯出 bug 線索）：\n" + "\n".join(
            f"  · {d}" for d in details
        )

    # -------------------------------------------------------- mode switching

    def _on_mode_action(self):
        act = self.mode_action_group.checkedAction()
        if act is None:
            return
        mode_id = act.data()
        self._switch_mode(mode_id, persist=True)

    def _switch_mode(self, mode_id: str, persist: bool):
        target = next((m for m in self.modes if m.id == mode_id), None)
        if target is None:
            return
        log.info("切換標註模式：%s -> %s", self.mode.id, mode_id)
        if self.doc is not None and self.doc.dirty:
            reply = QMessageBox.question(
                self, "切換模式",
                "切換標註模式可能讓既有標籤對應不到。建議先存檔。是否仍要切換？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                # revert the menu check
                for a in self.mode_action_group.actions():
                    a.setChecked(a.data() == self.mode.id)
                return
        self.mode = target
        self.concept_panel.setVisible(self.mode.id == "term_definition")
        if persist:
            self.prefs.active_mode_id = target.id
            try:
                self.prefs.save()
            except Exception:
                pass
        self._populate_annotate_menu()
        self._populate_label_buttons()
        if self.doc is not None:
            self.editor.attach(self.doc, self.mode, self.prefs)
        self._refresh_status()
        self._update_window_title()

    def _open_modes_dir(self):
        d = cfgmod.annotation_modes_dir()
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(d)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f"open '{d}'")
        else:
            os.system(f"xdg-open '{d}'")

    # ------------------------------------------------------------ misc UI

    def _build_context_menu(self, menu: QMenu, ann_id: Optional[str],
                            sel_start: int, sel_end: int):
        if self.doc is None:
            return
        has_selection = sel_end > sel_start

        # 如果有選取文字，在選單最上方加入「複製」選項
        if has_selection:
            act_copy = QAction("複製選取文字", menu)
            act_copy.setShortcut("Ctrl+C")  # 提示使用者亦可用快捷鍵
            act_copy.triggered.connect(self.editor.copy)  # 直接呼叫 QTextEdit 內建的複製功能
            menu.addAction(act_copy)
            menu.addSeparator()

        # If hovering over an annotation, show its info.
        if ann_id is not None:
            ann = self.doc.find_annotation(ann_id)
            if ann is not None:
                lbl_lines = []
                for g in self.mode.groups:
                    if self.mode.id == "term_definition" and g.id == "entity":
                        continue
                    lid = ann.labels.get(g.id)
                    if lid:
                        lb = g.label(lid)
                        if lb:
                            lbl_lines.append(f"{g.name}：{lb.name}")
                if lbl_lines:
                    head = QAction("　|　".join(lbl_lines), menu)
                    head.setEnabled(False)
                    menu.addAction(head)
                    menu.addSeparator()

        # 「專業術語」常用於快速標注，直接放在右鍵第一層，
        # 不必再展開「實體」子選單。
        if self.mode.id == "term_definition":
            term_group = self.mode.group("entity")
            term_label = term_group.label("term") if term_group else None
            if term_group is not None and term_label is not None:
                act_term = QAction(
                    _swatch_icon(term_label.color),
                    "專業術語",
                    menu,
                )
                act_term.triggered.connect(
                    self._make_label_handler(term_group.id, term_label.id)
                )
                menu.addAction(act_term)

        if ann_id is not None and self.mode.id == "term_definition":
            ann = self.doc.find_annotation(ann_id)
            if (
                ann is not None
                and ann.concept_id in self.doc.concepts
            ):
                act_edit_concept = QAction("編輯概念…", menu)
                act_edit_concept.triggered.connect(
                    lambda _checked=False, concept_id=ann.concept_id:
                    self._edit_concept(concept_id)
                )
                menu.addAction(act_edit_concept)
            if ann is not None:
                menu.addSeparator()

        for g in self.mode.groups:
            if self.mode.id == "term_definition" and g.id == "entity":
                continue
            sub = menu.addMenu(g.name)
            for lb in g.labels:
                shortcut = self.prefs.label_shortcut(g.id, lb.id, lb.shortcut)
                text = lb.name + (f"\t{shortcut}" if shortcut else "")
                act = QAction(_swatch_icon(lb.color), text, sub)
                act.triggered.connect(self._make_label_handler(g.id, lb.id))
                sub.addAction(act)
            sub.addSeparator()
            act_clear = QAction(f"清除「{g.name}」", sub)
            act_clear.triggered.connect(self._make_clear_group_handler(g.id))
            sub.addAction(act_clear)

        menu.addSeparator()
        act_remove = QAction("刪除此處標註", menu)
        act_remove.triggered.connect(self.action_remove_annotation_at_cursor)
        if not has_selection and ann_id is None:
            act_remove.setEnabled(False)
        menu.addAction(act_remove)

    # ----------------------------------------------- 反白後自動跳出快速選單

    def _show_quick_label_popup(self, global_pos: QPoint):
        """滑鼠拖選結束時呼叫；在選取附近彈出緊湊水平的標籤選單。

        以 prefs 開關控制；沒開檔案時不跳。
        """
        if not self.prefs.behavior.get("auto_popup_on_selection", True):
            return
        if self.doc is None:
            return

        menu = QMenu(self)

        # 直接攔截選單的鍵盤事件，強行捕捉 Ctrl+C
        def _menu_key_press(event):
            if event.matches(QKeySequence.Copy):
                self.editor.copy()
                menu.close()
            else:
                QMenu.keyPressEvent(menu, event)
        menu.keyPressEvent = _menu_key_press

        wa = QWidgetAction(menu)
        wa.setDefaultWidget(self._make_quick_label_widget(menu))
        menu.addAction(wa)

        # 在快速選單中加入複製選項，並攔截 Ctrl+C
        menu.addSeparator()
        act_copy = QAction("複製選取文字", menu)
        act_copy.setShortcut("Ctrl+C")
        # 複製文字後，順便關閉這個小選單，讓畫面保持乾淨
        act_copy.triggered.connect(lambda: (self.editor.copy(), menu.close()))
        menu.addAction(act_copy)

        # 避開剛放開滑鼠的位置，往下偏 8px。Qt 會自動處理畫面邊界。
        menu.exec(global_pos + QPoint(0, 8))

    def _make_quick_label_widget(self, host_menu: QMenu) -> QWidget:
        """建立水平緊湊版的標籤按鈕區塊；每個群組一列。

        按鈕樣式直接複用 :meth:`_button_style_for`；點擊行為走
        :meth:`_make_label_handler`，套用完關閉外層 ``host_menu``。
        """
        frame = QFrame()
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        for gi, g in enumerate(self.mode.groups):
            if gi > 0:
                sep = QFrame()
                sep.setFrameShape(QFrame.HLine)
                sep.setFrameShadow(QFrame.Sunken)
                outer.addWidget(sep)
            row = QHBoxLayout()
            row.setSpacing(4)
            for lb in g.labels:
                btn = QPushButton(lb.name)
                btn.setCursor(Qt.PointingHandCursor)
                shortcut = self.prefs.label_shortcut(g.id, lb.id, lb.shortcut)
                tip = lb.name
                if shortcut:
                    tip += f"  ({shortcut})"
                btn.setToolTip(tip)
                btn.setStyleSheet(self._button_style_for(lb))
                handler = self._make_label_handler(g.id, lb.id)
                btn.clicked.connect(
                    lambda _checked=False, h=handler: (h(), host_menu.close())
                )
                row.addWidget(btn)
            outer.addLayout(row)
        return frame

    def _on_concept_clicked(self, concept_id: str):
        if self.doc is None:
            return

        linked_annotations = sorted(
            (
                ann
                for ann in self.doc.annotations
                if ann.concept_id == concept_id
            ),
            key=lambda ann: (ann.start, ann.end),
        )

        if not linked_annotations:
            self.status.showMessage(
                "這個概念目前沒有對應的術語標註。",
                4000,
            )
            return

        # 將概念連結的所有術語一起反白，並跳到第一筆。
        first_ann = linked_annotations[0]
        annotation_ids = [ann.id for ann in linked_annotations]
        self.concept_panel.select_concept(concept_id)

        # 將該筆標註設為目前操作中的標註。
        self._on_annotation_clicked(first_ann.id, preserve_concept_highlight=True)

        # 立即更新一次，讓概念點擊的反白狀態同步可見；延遲再套用一次，
        # 避免樹狀清單的點擊收尾事件搶回焦點。
        self._restore_concept_highlights(annotation_ids)

        # Run after the tree's mouse event has finished; otherwise it can
        # reclaim focus and hide the editor's active text selection.
        QTimer.singleShot(
            0,
            lambda ids=annotation_ids: self._restore_concept_highlights(ids),
        )

    def _restore_concept_highlights(self, annotation_ids: list[str]):
        if self.doc is None:
            return
        valid_ids = [
            ann_id
            for ann_id in annotation_ids
            if self.doc.find_annotation(ann_id) is not None
        ]
        if not valid_ids:
            return
        self.editor.highlight_annotations(valid_ids)
        self.editor.setFocus(Qt.OtherFocusReason)
        self.editor.viewport().update()

    def _on_annotation_clicked(
        self,
        ann_id: str,
        preserve_concept_highlight: bool = False,
    ):
        if self.doc is None:
            return

        ann = self.doc.find_annotation(ann_id)
        if ann is None:
            return

        if not preserve_concept_highlight:
            # A text click sets the editing target without painting stale
            # multi-occurrence highlights over the document.
            self.editor.highlight_annotations([], jump_to_first=False)

        # Select the complete clicked term so the current annotation is
        # immediately visible, rather than leaving only an insertion cursor.
        self.editor.jump_to_annotation(ann.id)

        if (
            ann.concept_id in self.doc.concepts
            and not preserve_concept_highlight
        ):
            # 正常情況下清單會在概念建立時刷新；若載入舊資料或清單狀態
            # 曾落後文件模型，點到術語時補刷新一次並選中對應概念。
            if not self.concept_panel.select_concept(ann.concept_id):
                self.concept_panel.refresh(self.doc)
                self.concept_panel.select_concept(ann.concept_id)

        # MainWindow 與 Editor 都記住目前選中的標註。
        self._active_annotation_id = ann.id
        self.editor.set_active_annotation(
            ann.id if preserve_concept_highlight else None
        )
        if preserve_concept_highlight:
            # Keep the document focused so the selected first occurrence and
            # all concept highlights remain visibly emphasized after a sidebar click.
            self.editor.setFocus(Qt.OtherFocusReason)
            self.editor.refresh_highlights()

        term_text = self.doc.text[
            ann.start:ann.end
        ]

        if ann.concept_id is not None:
            concept_name = self.doc.concepts.get(
                ann.concept_id,
                ann.concept_id,
            )
        else:
            concept_name = "尚未指定概念"

        if ann.broader_concept_id is not None:
            broader_name = self.doc.concepts.get(
                ann.broader_concept_id,
                ann.broader_concept_id,
            )
        else:
            broader_name = "尚未設定"

        # 取得這一筆術語自己的解釋內容。
        explanation_text = ann.explanation.replace("\r", " ").replace("\n", " ").strip()
        if len(explanation_text) > 50:
            explanation_text = explanation_text[:47] + "…"
        if not explanation_text:
            explanation_text = "尚未輸入"

        self.status.showMessage(
            f"術語：{term_text}　｜　"
            f"文件概念：{concept_name}　｜　"
            f"上位概念：{broader_name}　｜　"
            f"解釋：{explanation_text}",
            10000,
        )

    def _on_unannotated_text_clicked(self):
        """Clear the previous annotation selection after clicking plain text."""
        self._active_annotation_id = None
        self.editor.clear_annotation_highlights()
        self._refresh_status()

    def _on_selection_changed(self, s: int, e: int):
        if self.mode.id == "term_definition" and self.doc is not None and e > s:
            candidates = [
                ann for ann in self.doc.annotations
                if ann.concept_id in self.doc.concepts
                and ann.start < e and ann.end > s
            ]
            if candidates:
                # Prefer an exact match, then the narrowest overlapping term.
                ann = min(
                    candidates,
                    key=lambda item: (
                        item.start != s or item.end != e,
                        item.end - item.start,
                    ),
                )
                self.concept_panel.select_concept(ann.concept_id)

        if e > s and self.doc is not None:
            seg = self.doc.text[s:e]
            preview = seg.replace("\r", " ").replace("\n", " ")
            if len(preview) > 60:
                preview = preview[:57] + "…"
            self.status.showMessage(f"選取 {e - s} 字：{preview}", 0)
        else:
            self._refresh_status()

    def _refresh_status(self):
        mode_text = f"模式：{self.mode.name}（{self.mode.id}）"
        self.status_mode_label.setText(mode_text)
        if self.doc is None:
            self.status.showMessage("尚未開啟檔案。可從右側清單或檔案 → 開啟。", 0)
        else:
            n = len(self.doc.annotations)
            grp_total = sum(len(a.labels) for a in self.doc.annotations)
            self.status.showMessage(
                f"已標註段落：{n}　|　標籤總數：{grp_total}　|　文字長度：{len(self.doc.text)}",
                0,
            )

    def _update_window_title(self):
        title = f"{__app_name__} - 法律文件標註工具"
        if self.doc is not None:
            name = self.doc.source_filename or (Path(self.doc.file_path).name if self.doc.file_path else "未命名")
            mark = "*" if self.doc.dirty else ""
            title = f"{name}{mark} - {title}"
        self.setWindowTitle(title)
        self.file_panel.set_dirty(bool(self.doc and self.doc.dirty))

    def _snapshot_document(self) -> Optional[Document]:
        return copy.deepcopy(self.doc) if self.doc is not None else None

    def _record_undo(self, before: Optional[Document], label: str):
        if before is None or self.doc is None or before == self.doc:
            return
        self._undo_stack.push(
            _DocumentSnapshotCommand(self, before, self.doc, label)
        )

    def _restore_document(self, snapshot: Document):
        if self.doc is None:
            return
        restored = copy.deepcopy(snapshot)
        self.doc.__dict__.clear()
        self.doc.__dict__.update(restored.__dict__)
        if self._active_annotation_id and not self.doc.find_annotation(
            self._active_annotation_id
        ):
            self._active_annotation_id = None
        self.editor.attach(self.doc, self.mode, self.prefs)
        self.concept_panel.refresh(self.doc)
        self._update_actions()
        self._refresh_status()
        self._update_window_title()

    def _update_actions(self):
        has_doc = self.doc is not None
        for a in (self.act_save, self.act_save_as, self.act_export, self.act_remove_ann):
            a.setEnabled(has_doc)

    # --------------------------------------------------------- autosave

    def _compute_autosave_path(self) -> Optional[Path]:
        """目前 doc 對應的 autosave 檔位置。

        命名格式：``<base>.autosave.YYYYMMDD_HHMMSS.lexa``，每次呼叫產生
        新時間戳；保留份數由 :meth:`_prune_autosaves` 控制。

        有 file_path 時放在原檔旁邊；尚未存過的新檔則退而求其次寫到
        log 資料夾，避免完全沒備份。若 ``file_path`` 本身指向某個
        autosave 檔（例如使用者透過開檔對話框直接挑了 autosave），會
        先還原成原 base 再加時間戳，避免 ``.autosave.autosave...`` 疊加。
        """
        if self.doc is None:
            return None
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if self.doc.file_path:
            base = _autosave_base_path(Path(self.doc.file_path))
            return base.with_name(f"{base.stem}.autosave.{stamp}.lexa")
        d = current_log_dir()
        if d is None:
            return None
        return d / f"untitled.autosave.{stamp}.lexa"

    def _list_autosaves_for(self, base_lexa: Path) -> list[Path]:
        """列出該 base .lexa 對應的所有 autosave 檔（含舊版無時間戳格式）。"""
        parent = base_lexa.parent
        if not parent.exists():
            return []
        base_stem = base_lexa.stem
        out: list[Path] = []
        try:
            entries = list(parent.iterdir())
        except OSError:
            return []
        for p in entries:
            if not p.is_file() or not p.name.lower().endswith(".lexa"):
                continue
            m = _AUTOSAVE_RE.match(p.name)
            if m and m.group("base") == base_stem:
                out.append(p)
        return out

    def _prune_autosaves(self, base_lexa: Path, keep: int = 2) -> None:
        """只保留最新 ``keep`` 份，其餘刪除。失敗只記 log 不拋例外。"""
        files = self._list_autosaves_for(base_lexa)
        try:
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for old in files[keep:]:
            try:
                old.unlink()
                log.info("已刪除舊 autosave：%s", old)
            except OSError:
                log.warning("刪除舊 autosave 失敗：%s", old, exc_info=True)

    def _autosave_companion(self, src_path: Path) -> Optional[Path]:
        """若 src_path 旁邊有比它新的 autosave 檔，回傳最新那份。

        autosave 檔本身不會再去找 companion（避免遞迴）。
        """
        if _is_autosave_name(src_path.name):
            return None
        if not src_path.exists():
            return None
        base = _autosave_base_path(src_path)  # 通常 == src_path
        candidates = self._list_autosaves_for(base)
        if not candidates:
            return None
        try:
            src_mtime = src_path.stat().st_mtime
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            newest = candidates[0]
            if newest.stat().st_mtime > src_mtime:
                return newest
        except OSError:
            return None
        return None

    def _autosave_tick(self):
        """QTimer 每 60 秒呼叫；只在 doc 有未存內容時寫入。"""
        if self.doc is None or not self.doc.dirty:
            return
        path = self._compute_autosave_path()
        if path is None:
            return
        try:
            # update_doc_state=False：不要因為 autosave 就把 dirty 清掉，
            # 使用者仍應該看到「未儲存」標記、仍應該主動存檔。
            iomod.save_lexa(self.doc, path, self.mode, update_doc_state=False)
            self._last_autosave_path = path
            log.info("autosave: %s", path)
            # 寫入後立刻修剪，保證磁碟上同 base 至多 2 份。
            if self.doc.file_path:
                base = _autosave_base_path(Path(self.doc.file_path))
                self._prune_autosaves(base, keep=2)
        except Exception:
            log.error("autosave 失敗：%s", path, exc_info=True)

    def _cleanup_autosave(self):
        """正式存檔成功 / 使用者捨棄變更後，把該 doc 對應的所有 autosave 都清掉。"""
        if self.doc is not None and self.doc.file_path:
            base = _autosave_base_path(Path(self.doc.file_path))
            # 防呆：若使用者透過 QFileDialog 直接開了某個 autosave 檔，
            # doc.file_path 就會指向 autosave 本身——這時不要把它一起刪掉。
            try:
                current = Path(self.doc.file_path).resolve()
            except OSError:
                current = None
            for old in self._list_autosaves_for(base):
                try:
                    if current is not None and old.resolve() == current:
                        continue
                except OSError:
                    pass
                try:
                    old.unlink()
                    log.info("已清除 autosave：%s", old)
                except OSError:
                    log.warning("刪除 autosave 失敗：%s", old, exc_info=True)
        else:
            # 沒有 file_path（未存過的新檔）→ 用 _last_autosave_path 兜底
            p = self._last_autosave_path
            if p is not None and p.exists():
                try:
                    p.unlink()
                    log.info("已清除 autosave：%s", p)
                except OSError:
                    log.warning("刪除 autosave 失敗：%s", p, exc_info=True)
        self._last_autosave_path = None

    # ----------------------------------------------------------- 診斷

    def _open_log_dir(self):
        d = current_log_dir()
        if d is None or not d.exists():
            QMessageBox.warning(
                self, "找不到 log 資料夾",
                "目前沒有可用的 log 資料夾（可能 logging 初始化失敗）。",
            )
            return
        if sys.platform.startswith("win"):
            os.startfile(d)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f"open '{d}'")
        else:
            os.system(f"xdg-open '{d}'")

    def _copy_diagnostics(self):
        """把版本/平台/log 路徑等資訊複製到剪貼簿，供使用者貼到回報訊息中。"""
        import platform as _platform
        lines = [
            f"LexiArbiter {__version__}",
            f"Python {sys.version.split()[0]}",
            f"Platform: {_platform.platform()}",
            f"app_root: {cfgmod.app_root()}",
            f"log_dir: {current_log_dir()}",
            f"active_mode: {self.mode.id} ({self.mode.name})",
            f"available_modes: {', '.join(m.id for m in self.modes)}",
            f"current_doc: {self.doc.file_path if self.doc else '(none)'}",
            f"dirty: {self.doc.dirty if self.doc else False}",
            f"annotations: {len(self.doc.annotations) if self.doc else 0}",
        ]
        text = "\n".join(lines)
        QApplication.clipboard().setText(text)
        QMessageBox.information(
            self, "已複製診斷資訊",
            "下列資訊已複製到剪貼簿，請連同 log 檔一起貼給開發者：\n\n" + text,
        )

    def _show_about(self):
        QMessageBox.about(
            self,
            f"關於 {__app_name__}",
            f"<h3>{__app_name__} {__version__}</h3>"
            "<p>法律文件多任務標註工具（MTL 訓練資料用）。</p>"
            "<p>讀取司法判決開放資料 JSON，輸出 <code>.lexa</code> 進度檔與 "
            "<code>.txt</code> 模型訓練檔。</p>",
        )

    # ------------------------------------------------------------ events

    def emergency_save(self) -> None:
        """程式崩潰時由 crash hook 呼叫，嘗試將未儲存的標註寫到備份檔。

        不顯示任何 UI，結果只寫 log。
        """
        if self.doc is None or not self.doc.dirty:
            return
        # 已有檔案路徑 → 寫在原檔旁邊；否則寫到 log 資料夾，
        # 保證落點是先前驗證過可寫的位置。
        if self.doc.file_path:
            backup_path = Path(self.doc.file_path).with_suffix(".crash_backup.lexa")
        else:
            d = current_log_dir()
            if d is None:
                log.error("緊急存檔失敗：沒有 file_path 也沒有 log_dir 可寫")
                return
            from datetime import datetime
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = d / f"emergency_{stamp}.crash_backup.lexa"
        try:
            iomod.save_lexa(self.doc, backup_path, self.mode, update_doc_state=False)
            log.info("緊急存檔成功：%s", backup_path)
        except Exception:
            log.error("緊急存檔失敗：%s", backup_path, exc_info=True)

    def closeEvent(self, ev):
        if not self._confirm_unsaved():
            ev.ignore()
            return
        super().closeEvent(ev)
