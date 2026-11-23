"""Concept management UI components."""
import uuid
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget,
    QListWidgetItem, QTreeWidget, QTreeWidgetItem,
    QAbstractItemView, QLineEdit, QPushButton,
    QLabel, QWidget, QMessageBox, QComboBox, QPlainTextEdit, QCheckBox,
    QFormLayout, QMenu
)

class ConceptSelectionDialog(QDialog):
    """彈出視窗：讓使用者從現有概念挑選，或輸入新概念。"""
    def __init__(
        self,
        concepts: dict[str, str],
        default_text: str = "",
        parent=None,
        allow_new: bool = True,
        broader_by_concept: dict[str, str] | None = None,
    ):
        super().__init__(parent)
        self.allow_new = allow_new
        self._updating_tree = False
        self.setWindowTitle("選擇或新增對應概念")
        self.resize(420, 500)
        
        layout = QVBoxLayout(self)
        
        layout.addWidget(QLabel("1. 從概念階層中選擇："))
        self.tree_widget = QTreeWidget()
        self.tree_widget.setHeaderHidden(True)
        self.tree_widget.setSelectionMode(
            QAbstractItemView.SingleSelection
        )
        self._concept_map = {
            name: cid for cid, name in concepts.items()
        }
        self._tree_items: dict[str, QTreeWidgetItem] = {}
        self._build_concept_tree(
            concepts,
            broader_by_concept or {},
        )
        self.tree_widget.expandAll()
        layout.addWidget(self.tree_widget)

        self.input_label = QLabel("2. 或直接輸入新概念：")
        layout.addWidget(self.input_label)
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("輸入新術語概念...")
        # 自動將反白的文字填入輸入框
        self.input_edit.setText(default_text) 
        # 選取框內所有文字，方便使用者如果不滿意可以直接打字覆蓋
        self.input_edit.selectAll()
        layout.addWidget(self.input_edit)

        # 有些流程（例如「要被設定的子概念」）只能選既有概念。
        # 直接隱藏新增欄位，避免使用者輸入後才收到拒絕訊息。
        self.input_label.setVisible(allow_new)
        self.input_edit.setVisible(allow_new)

        # 按鈕區
        btn_layout = QHBoxLayout()
        self.btn_ok = QPushButton("確定 (Enter)")
        self.btn_ok.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold; padding: 6px; border-radius: 4px;")
        self.btn_ok.clicked.connect(self.accept)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setStyleSheet("background-color: #546E7A; color: white; padding: 6px; border-radius: 4px;")
        self.btn_cancel.clicked.connect(self.reject)

        # 將按鈕推到對話框的右下角
        btn_layout.addStretch()
        
        # 將 OK 放在前面 (左邊)，Cancel 放在後面 (右邊)
        btn_layout.addWidget(self.btn_ok)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)

        # 階層清單採單選勾選；點文字或方框都會選取該概念。
        self.tree_widget.itemClicked.connect(
            self._on_tree_item_clicked
        )
        self.tree_widget.itemChanged.connect(
            self._on_tree_item_changed
        )
        self.tree_widget.itemDoubleClicked.connect(
            lambda _item, _column: self.accept()
        )
        self.input_edit.textEdited.connect(
            self._on_input_edited
        )

    def _build_concept_tree(
        self,
        concepts: dict[str, str],
        broader_by_concept: dict[str, str],
    ):
        """依 concept -> broader concept 關係建立可展開的概念樹。"""
        parent_by_child = {
            child_id: broader_id
            for child_id, broader_id in broader_by_concept.items()
            if child_id in concepts
            and broader_id in concepts
            and child_id != broader_id
        }

        # 防止舊資料中的循環關係讓概念完全無法顯示；遇到循環時，
        # 將其中一個節點提升為根節點。
        for concept_id in list(parent_by_child):
            seen = {concept_id}
            current = parent_by_child.get(concept_id)
            while current is not None:
                if current in seen:
                    parent_by_child.pop(concept_id, None)
                    break
                seen.add(current)
                current = parent_by_child.get(current)

        children_by_parent: dict[str, list[str]] = {}
        for child_id, parent_id in parent_by_child.items():
            children_by_parent.setdefault(parent_id, []).append(
                child_id
            )

        def add_item(
            concept_id: str,
            parent_item: QTreeWidgetItem | None,
        ):
            item = QTreeWidgetItem([concepts[concept_id]])
            item.setData(0, Qt.UserRole, concept_id)
            item.setFlags(
                item.flags()
                | Qt.ItemIsUserCheckable
                | Qt.ItemIsSelectable
                | Qt.ItemIsEnabled
            )
            item.setCheckState(0, Qt.Unchecked)

            parent_id = parent_by_child.get(concept_id)
            if parent_id is None:
                item.setToolTip(0, "最上層概念")
            else:
                item.setToolTip(
                    0,
                    f"上位概念：{concepts[parent_id]}",
                )

            if parent_item is None:
                self.tree_widget.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
            self._tree_items[concept_id] = item

            child_ids = children_by_parent.get(concept_id, [])
            for child_id in sorted(
                child_ids,
                key=lambda cid: concepts[cid],
            ):
                add_item(child_id, item)

        root_ids = [
            concept_id
            for concept_id in concepts
            if concept_id not in parent_by_child
        ]
        for concept_id in sorted(
            root_ids,
            key=lambda cid: concepts[cid],
        ):
            add_item(concept_id, None)

    def _select_tree_item(self, item: QTreeWidgetItem):
        self._updating_tree = True
        try:
            for other in self._tree_items.values():
                other.setCheckState(
                    0,
                    Qt.Checked if other is item else Qt.Unchecked,
                )
            self.tree_widget.setCurrentItem(item)
        finally:
            self._updating_tree = False

        if self.allow_new:
            self.input_edit.setText(item.text(0))

    def _on_tree_item_clicked(
        self,
        item: QTreeWidgetItem,
        _column: int,
    ):
        self._select_tree_item(item)

    def _on_tree_item_changed(
        self,
        item: QTreeWidgetItem,
        _column: int,
    ):
        if self._updating_tree:
            return
        if item.checkState(0) == Qt.Checked:
            self._select_tree_item(item)

    def _on_input_edited(self, text: str):
        """輸入新名稱時取消舊勾選，避免畫面同時表示兩種結果。"""
        if not self.allow_new:
            return
        current = self.tree_widget.currentItem()
        if current is None or text.strip() == current.text(0):
            return
        self._updating_tree = True
        try:
            current.setCheckState(0, Qt.Unchecked)
            self.tree_widget.clearSelection()
        finally:
            self._updating_tree = False

    def accept(self):
        if not self.allow_new:
            if self.tree_widget.currentItem() is None:
                QMessageBox.information(
                    self,
                    "請選擇既有概念",
                    "請從上方清單選擇要設定的概念。",
                )
                self.tree_widget.setFocus()
                return

            super().accept()
            return

        text = self.input_edit.text().strip()

        if not text:
            QMessageBox.information(
                self,
                "概念名稱不可空白",
                "請選擇既有概念，或輸入新的概念名稱。",
            )
            self.input_edit.setFocus()
            return

        super().accept()


    def get_result(self) -> tuple[str | None, str | None]:
        """回傳 (已存在的 ID, 新增的名稱)。若輸入框為空則回傳 (None, None)。"""
        if not self.allow_new:
            item = self.tree_widget.currentItem()
            if item is None:
                return None, None
            return item.data(0, Qt.UserRole), None

        text = self.input_edit.text().strip()
        if not text:
            return None, None
        if text in self._concept_map:
            return self._concept_map[text], None
        return None, text


class ExplanationDialog(QDialog):
    """為目前選取的單筆術語手動輸入解釋。"""
    def __init__(self, term: str, explanation: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"編輯「{term}」的解釋")
        self.resize(520, 300)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"術語：{term}\n請直接輸入此筆術語的解釋："))
        self.edit = QPlainTextEdit()
        self.edit.setPlainText(explanation)
        self.edit.setPlaceholderText("輸入解釋文字…")
        layout.addWidget(self.edit)
        buttons = QHBoxLayout()
        buttons.addStretch()
        ok = QPushButton("確定")
        cancel = QPushButton("取消")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def result_text(self) -> str:
        return self.edit.toPlainText().strip()


class ConceptEditDialog(QDialog):
    occurrences_selected = Signal(object)
    PICK_PARENT_FROM_TEXT = 2

    """顯示並編輯概念名稱、上位概念與指定術語的解釋。"""
    def __init__(
        self,
        concept_id: str,
        concept_name: str,
        concepts: dict[str, str],
        parent_by_concept: dict[str, str],
        annotations: list,
        text: str,
        selected_annotation_id: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("編輯概念")
        self.resize(540, 440)
        self._annotations = annotations
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(concept_name)
        form.addRow("文件概念：", self.name_edit)

        self.parent_combo = QComboBox()
        self.parent_combo.addItem("（最上層概念）", None)
        excluded = {concept_id}
        # Avoid selecting any descendant as the parent.
        changed = True
        while changed:
            changed = False
            for child, broader in parent_by_concept.items():
                if child not in excluded and broader in excluded:
                    excluded.add(child)
                    changed = True
        for cid, name in sorted(concepts.items(), key=lambda pair: pair[1]):
            if cid not in excluded:
                self.parent_combo.addItem(name, cid)
        current_parent = parent_by_concept.get(concept_id)
        if current_parent == concept_id:
            current_parent = None
        idx = self.parent_combo.findData(current_parent)
        self.parent_combo.setCurrentIndex(max(0, idx))
        parent_box = QWidget()
        parent_layout = QHBoxLayout(parent_box)
        parent_layout.setContentsMargins(0, 0, 0, 0)
        parent_layout.addWidget(self.parent_combo, 1)
        self.pick_parent_button = QPushButton("從文件點選")
        self.pick_parent_button.clicked.connect(
            lambda: self.done(self.PICK_PARENT_FROM_TEXT)
        )
        parent_layout.addWidget(self.pick_parent_button)
        form.addRow("上位概念：", parent_box)

        self.occurrence_list = QListWidget()
        self._occurrence_ids: list[str] = []
        self._annotations_by_id = {}
        for ann in annotations:
            if ann.concept_id != concept_id:
                continue
            term = text[ann.start:ann.end]
            self._occurrence_ids.append(ann.id)
            self._annotations_by_id[ann.id] = ann
            item = QListWidgetItem(
                self._format_occurrence_context(text, ann, term)
            )
            item.setData(Qt.UserRole, ann.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.occurrence_list.addItem(item)
        self.select_all_checkbox = QCheckBox("全選（一起編輯所有解釋）")
        occurrence_box = QWidget()
        occurrence_layout = QVBoxLayout(occurrence_box)
        occurrence_layout.setContentsMargins(0, 0, 0, 0)
        occurrence_layout.addWidget(self.select_all_checkbox)
        occurrence_layout.addWidget(self.occurrence_list)
        form.addRow("編輯哪些術語：", occurrence_box)
        layout.addLayout(form)
        layout.addWidget(QLabel("解釋（修改後會覆寫所有勾選項的原解釋）："))
        self.explanation_edit = QPlainTextEdit()
        self.explanation_edit.setPlaceholderText("勾選一筆或多筆術語後，在此輸入解釋…")
        layout.addWidget(self.explanation_edit)
        self._explanation_dirty = False
        self.explanation_edit.textChanged.connect(self._mark_explanation_dirty)
        self.occurrence_list.itemChanged.connect(self._on_occurrence_check_changed)
        self.select_all_checkbox.stateChanged.connect(self._set_all_occurrences)
        if selected_annotation_id:
            for index in range(self.occurrence_list.count()):
                item = self.occurrence_list.item(index)
                if item.data(Qt.UserRole) == selected_annotation_id:
                    item.setCheckState(Qt.Checked)
                    break
        if not self.selected_annotation_ids() and self.occurrence_list.count():
            self.occurrence_list.item(0).setCheckState(Qt.Checked)
        self._sync_occurrence_selection()

        buttons = QHBoxLayout()
        buttons.addStretch()
        ok = QPushButton("儲存")
        cancel = QPushButton("取消")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    @staticmethod
    def _format_occurrence_context(text: str, ann, term: str, context_chars: int = 6) -> str:
        """Show a short, same-line context and the term's source line number."""
        line_start = text.rfind("\n", 0, ann.start) + 1
        line_end = text.find("\n", ann.end)
        if line_end < 0:
            line_end = len(text)

        before_start = max(line_start, ann.start - context_chars)
        after_end = min(line_end, ann.end + context_chars)
        before = text[before_start:ann.start]
        after = text[ann.end:after_end]
        before = before.replace("\r", " ").replace("\t", " ")
        after = after.replace("\r", " ").replace("\t", " ")

        leading_ellipsis = "…" if before_start > line_start else ""
        trailing_ellipsis = "…" if after_end < line_end else ""
        line_number = text.count("\n", 0, ann.start) + 1
        return (
            f"{leading_ellipsis}{before}【{term}】{after}"
            f"{trailing_ellipsis}（第 {line_number} 行）"
        )

    def selected_annotation_ids(self) -> list[str]:
        return [
            self.occurrence_list.item(index).data(Qt.UserRole)
            for index in range(self.occurrence_list.count())
            if self.occurrence_list.item(index).checkState() == Qt.Checked
        ]

    def set_parent_concept(self, concept_id: str) -> bool:
        index = self.parent_combo.findData(concept_id)
        if index < 0:
            return False
        self.parent_combo.setCurrentIndex(index)
        return True

    def _mark_explanation_dirty(self):
        self._explanation_dirty = True

    def _on_occurrence_check_changed(self, _item):
        checked = self.selected_annotation_ids()
        self.select_all_checkbox.blockSignals(True)
        self.select_all_checkbox.setChecked(
            bool(self._occurrence_ids) and len(checked) == len(self._occurrence_ids)
        )
        self.select_all_checkbox.blockSignals(False)
        self._sync_occurrence_selection()

    def _set_all_occurrences(self, state):
        # QCheckBox.stateChanged emits an int, while Qt.Checked is an enum;
        # comparing them directly is false in PySide6 even when state == 2.
        checked = bool(getattr(state, "value", state))
        self.occurrence_list.blockSignals(True)
        for index in range(self.occurrence_list.count()):
            self.occurrence_list.item(index).setCheckState(
                Qt.Checked if checked else Qt.Unchecked
            )
        self.occurrence_list.blockSignals(False)
        self._sync_occurrence_selection()

    def _sync_occurrence_selection(self):
        selected = self.selected_annotation_ids()
        self._explanation_dirty = False
        if len(selected) == 1:
            explanation = self._annotations_by_id[selected[0]].explanation
        elif selected:
            values = {self._annotations_by_id[ann_id].explanation for ann_id in selected}
            explanation = values.pop() if len(values) == 1 else ""
        else:
            explanation = ""
        self.explanation_edit.blockSignals(True)
        self.explanation_edit.setPlainText(explanation or "")
        self.explanation_edit.blockSignals(False)
        self.explanation_edit.setPlaceholderText(
            "已勾選多筆；輸入後會覆寫這些筆的原解釋並統一內容…"
            if len(selected) > 1 else (
                "輸入內容後會覆寫這筆術語原有的解釋…"
                if selected else "請先勾選要編輯的術語…"
            )
        )
        self.occurrences_selected.emit(selected)

    def result_values(self) -> tuple[str, str | None, list[str], str | None]:
        return (
            self.name_edit.text().strip(),
            self.parent_combo.currentData(),
            self.selected_annotation_ids(),
            self.explanation_edit.toPlainText().strip() if self._explanation_dirty else None,
        )


class ConceptSidebar(QWidget):
    """可收合的文件概念階層，提供新增、編輯與刪除入口。"""
    concept_clicked = Signal(str)
    edit_requested = Signal(str)
    delete_requested = Signal(str)
    add_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        
        title = QLabel("文件概念清單 (Concepts)")
        f = title.font()
        f.setBold(True)
        title.setFont(f)
        
        self.add_button = QPushButton("＋ 新增概念")
        self.add_button.clicked.connect(self.add_requested.emit)
        self.list_widget = QTreeWidget()
        self.list_widget.setHeaderHidden(True)
        self.list_widget.itemClicked.connect(
            self._on_item_clicked
        )
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_context_menu)
        
        layout.addWidget(title)
        layout.addWidget(self.add_button)
        layout.addWidget(self.list_widget)

    def refresh(self, doc):
        self.list_widget.clear()
        if not doc or not hasattr(doc, 'concepts'):
            return
        parent_map = dict(getattr(doc, "broader_concepts", {}) or {})
        for ann in doc.annotations:
            if ann.concept_id in doc.concepts and ann.broader_concept_id in doc.concepts:
                parent_map.setdefault(ann.concept_id, ann.broader_concept_id)
        parents = {
            cid: pid for cid, pid in parent_map.items()
            if cid in doc.concepts and pid in doc.concepts and cid != pid
        }
        # Break legacy cycles so every concept remains visible.
        for cid in list(parents):
            seen = {cid}
            current = parents.get(cid)
            while current is not None:
                if current in seen:
                    parents.pop(cid, None)
                    break
                seen.add(current)
                current = parents.get(current)
        children: dict[str, list[str]] = {}
        for cid, pid in parents.items():
            children.setdefault(pid, []).append(cid)

        concept_order = {
            concept_id: index
            for index, concept_id in enumerate(doc.concepts)
        }
        first_occurrence: dict[str, int] = {}
        for ann in doc.annotations:
            if ann.concept_id not in doc.concepts:
                continue
            first_occurrence[ann.concept_id] = min(
                first_occurrence.get(ann.concept_id, ann.start),
                ann.start,
            )

        def concept_sort_key(concept_id: str):
            return (
                first_occurrence.get(concept_id, float("inf")),
                concept_order[concept_id],
            )

        def add_item(cid: str, parent_item=None):
            item = QTreeWidgetItem([doc.concepts[cid]])
            item.setData(0, Qt.UserRole, cid)
            if parent_item is None:
                self.list_widget.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
            for child in sorted(
                children.get(cid, []),
                key=concept_sort_key,
            ):
                add_item(child, item)

        roots = [cid for cid in doc.concepts if cid not in parents]
        for cid in sorted(roots, key=concept_sort_key):
            add_item(cid)

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int = 0):
        concept_id = item.data(0, Qt.UserRole)
        if concept_id:
            self.concept_clicked.emit(concept_id)

    def select_concept(self, concept_id: str) -> bool:
        """Reveal and select a concept in the hierarchy tree."""
        root = self.list_widget.invisibleRootItem()
        stack = [root.child(i) for i in range(root.childCount())]
        while stack:
            item = stack.pop()
            if item.data(0, Qt.UserRole) == concept_id:
                parent = item.parent()
                while parent is not None:
                    self.list_widget.expandItem(parent)
                    parent = parent.parent()
                self.list_widget.setCurrentItem(item)
                item.setSelected(True)
                self.list_widget.scrollToItem(item)
                self.list_widget.setFocus(Qt.OtherFocusReason)
                return True
            stack.extend(
                item.child(i) for i in range(item.childCount())
            )
        return False

    def _show_context_menu(self, position):
        item = self.list_widget.itemAt(position)
        if item is None:
            return
        concept_id = item.data(0, Qt.UserRole)
        menu = QMenu(self)
        edit = menu.addAction("編輯概念、上位概念與解釋…")
        delete = menu.addAction("刪除概念…")
        chosen = menu.exec(self.list_widget.viewport().mapToGlobal(position))
        if chosen is edit:
            self.edit_requested.emit(concept_id)
        elif chosen is delete:
            self.delete_requested.emit(concept_id)
