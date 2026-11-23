import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QApplication, QMessageBox

from lexiarbiter.app import MainWindow
from lexiarbiter.core.config import UserPreferences, list_annotation_modes
from lexiarbiter.core.models import Annotation, Document
from lexiarbiter.widgets.concept_panel import (
    ConceptEditDialog,
    ConceptSelectionDialog,
    ConceptSidebar,
)
from lexiarbiter.widgets.editor import AnnotationEditor


class _FakeTextControl:
    def setText(self, _text):
        pass

    def setPlaceholderText(self, _text):
        pass


class _FakeExplanationDialog:
    value = ""

    def __init__(self, *_args, **_kwargs):
        pass

    def exec(self):
        return True

    def result_text(self):
        return self.value

    def setPlaceholderText(self, _text):
        pass


class _FakeConceptDialog:
    results = []

    def __init__(self, *args, **kwargs):
        self.input_label = _FakeTextControl()
        self.input_edit = _FakeTextControl()

    def setWindowTitle(self, _title):
        pass

    def exec(self):
        return True

    def get_result(self):
        return self.results.pop(0)


class TermFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.mode = next(
            mode
            for mode in list_annotation_modes()
            if mode.id == "term_definition"
        )

    def test_edit_concept_occurrence_selection_jumps_and_highlights(self):
        text = "無因管理 middle 無因管理"
        annotations = [
            Annotation(0, 4, {"entity": "term"}, id="ann_6", concept_id="C"),
            Annotation(12, 16, {"entity": "term"}, id="ann_9", concept_id="C"),
        ]
        doc = Document(
            text=text,
            annotations=annotations,
            concepts={"C": "無因管理"},
        )
        editor = AnnotationEditor()
        editor.attach(doc, self.mode, UserPreferences.load())
        dialog = ConceptEditDialog("C", "無因管理", doc.concepts, {}, annotations, text)
        dialog.occurrences_selected.connect(editor.highlight_annotations)

        for index, expected_start in ((1, 12), (0, 0), (1, 12)):
            for other_index in range(dialog.occurrence_list.count()):
                dialog.occurrence_list.item(other_index).setCheckState(Qt.Unchecked)
            dialog.occurrence_list.item(index).setCheckState(Qt.Checked)
            cursor = editor.textCursor()
            self.assertEqual(cursor.selectedText(), "無因管理")
            self.assertEqual(
                (cursor.selectionStart(), cursor.selectionEnd()),
                (expected_start, expected_start + 4),
            )
            dialog.occurrence_list.item(index).setCheckState(Qt.Unchecked)

        dialog.close()
        editor.close()

    def test_concept_click_highlights_all_matching_occurrences(self):
        annotations = [
            Annotation(0, 4, {"entity": "term"}, id="a", concept_id="C"),
            Annotation(12, 16, {"entity": "term"}, id="b", concept_id="C"),
        ]
        window = MainWindow()
        window.doc = Document(
            text="無因管理 middle 無因管理",
            annotations=annotations,
            concepts={"C": "無因管理"},
        )
        window.editor.attach(window.doc, self.mode, UserPreferences.load())
        window._on_concept_clicked("C")
        cursor = window.editor.textCursor()
        self.assertEqual(cursor.selectedText(), "無因管理")
        self.assertEqual(cursor.selectionStart(), 0)
        self.assertEqual(window.editor._concept_highlight_ids, {"a", "b"})
        highlighted_ranges = {
            (selection.cursor.selectionStart(), selection.cursor.selectionEnd())
            for selection in window.editor.extraSelections()
            if selection.format.background().color().name().lower() == "#ffeb3b"
        }
        # 第一筆使用系統文字選取色，其餘同概念出現位置使用黃色。
        self.assertEqual(highlighted_ranges, {(12, 16)})
        window.doc.dirty = False
        window.close()

    def test_clicking_annotated_text_reveals_concept_in_sidebar(self):
        ann = Annotation(0, 4, {"entity": "term"}, id="a", concept_id="C_child")
        doc = Document(
            text="term",
            annotations=[ann],
            concepts={"C_root": "Root", "C_parent": "Parent", "C_child": "Child"},
            broader_concepts={
                "C_root": "C_root",
                "C_parent": "C_root",
                "C_child": "C_parent",
            },
        )
        window = MainWindow()
        window.doc = doc
        window.concept_panel.refresh(doc)
        window._on_annotation_clicked(ann.id)
        selected = window.concept_panel.list_widget.currentItem()
        self.assertIsNotNone(selected)
        self.assertEqual(selected.data(0, Qt.UserRole), "C_child")
        self.assertEqual(selected.parent().data(0, Qt.UserRole), "C_parent")
        window.doc.dirty = False
        window.close()

    def test_clicking_term_sets_edit_target_without_highlighting(self):
        annotations = [
            Annotation(0, 4, {"entity": "term"}, id="first", concept_id="C"),
            Annotation(12, 16, {"entity": "term"}, id="second", concept_id="C"),
        ]
        doc = Document(
            text="term middle term",
            annotations=annotations,
            concepts={"C": "Term"},
        )
        window = MainWindow()
        window.doc = doc
        window.mode = self.mode
        window.concept_panel.refresh(doc)
        window.editor.attach(doc, self.mode, window.prefs)

        window._on_concept_clicked("C")
        window._on_annotation_clicked("second")

        self.assertEqual(window._active_annotation_id, "second")
        self.assertEqual(window.editor._concept_highlight_ids, set())
        self.assertIsNone(window.editor._active_annotation_id)
        window.doc.dirty = False
        window.close()

    def test_undo_restores_deleted_annotation_and_redo_removes_it(self):
        ann = Annotation(0, 4, {"entity": "term"}, id="a", concept_id="C")
        doc = Document(text="term", annotations=[ann], concepts={"C": "Term"})
        window = MainWindow()
        window.doc = doc
        window.mode = self.mode
        window.editor.attach(doc, self.mode, window.prefs)
        window._on_concept_clicked("C")

        window.action_remove_annotation_at_cursor()
        self.assertIsNone(doc.find_annotation("a"))
        self.assertEqual(window.editor._concept_highlight_ids, set())
        self.assertFalse(window.editor.textCursor().hasSelection())
        self.assertTrue(window.act_undo.isEnabled())

        window.act_undo.trigger()
        restored = doc.find_annotation("a")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.labels, {"entity": "term"})

        window._undo_stack.redo()
        self.assertIsNone(doc.find_annotation("a"))
        window.doc.dirty = False
        window.close()

    def test_select_all_checks_every_occurrence(self):
        annotations = [
            Annotation(0, 4, {"entity": "term"}, id="ann_6", concept_id="C"),
            Annotation(10, 14, {"entity": "term"}, id="ann_9", concept_id="C"),
        ]
        dialog = ConceptEditDialog(
            "C", "無因管理", {"C": "無因管理"}, {}, annotations,
            "無因管理 six 無因管理",
        )
        dialog.select_all_checkbox.setChecked(True)
        self.assertEqual(dialog.selected_annotation_ids(), ["ann_6", "ann_9"])
        self.assertTrue(all(
            dialog.occurrence_list.item(index).checkState() == Qt.Checked
            for index in range(dialog.occurrence_list.count())
        ))
        dialog.explanation_edit.setPlainText("統一後的解釋")
        self.assertEqual(
            dialog.result_values()[2:],
            (["ann_6", "ann_9"], "統一後的解釋"),
        )
        dialog.select_all_checkbox.setChecked(False)
        self.assertEqual(dialog.selected_annotation_ids(), [])
        dialog.close()

    def test_text_selection_reveals_and_selects_matching_concept(self):
        annotations = [
            Annotation(0, 4, {"entity": "term"}, id="ann_6", concept_id="C_child"),
            Annotation(12, 16, {"entity": "term"}, id="ann_9", concept_id="C_child"),
        ]
        doc = Document(
            text="term middle term",
            annotations=annotations,
            concepts={"C_root": "Root", "C_parent": "Parent", "C_child": "Child"},
            broader_concepts={
                "C_root": "C_root",
                "C_parent": "C_root",
                "C_child": "C_parent",
            },
        )
        window = MainWindow()
        window.doc = doc
        window.concept_panel.refresh(doc)

        window._on_selection_changed(12, 16)

        selected = window.concept_panel.list_widget.currentItem()
        self.assertIsNotNone(selected)
        self.assertEqual(selected.data(0, Qt.UserRole), "C_child")
        self.assertEqual(selected.parent().data(0, Qt.UserRole), "C_parent")
        self.assertEqual(selected.parent().parent().data(0, Qt.UserRole), "C_root")
        self.assertTrue(selected.isSelected())
        self.assertTrue(selected.parent().isExpanded())
        self.assertTrue(selected.parent().parent().isExpanded())
        window.doc.dirty = False
        window.close()

    def test_new_broader_concept_is_accepted(self):
        window = MainWindow()
        annotation = Annotation(
            start=0,
            end=4,
            labels={"entity": "term"},
            concept_id="C_child",
        )
        window.doc = Document(
            text="損害賠償",
            annotations=[annotation],
            concepts={"C_child": "損害賠償"},
        )
        window.mode = self.mode

        _FakeConceptDialog.results = [
            ("C_child", None),
            (None, "債務不履行責任"),
        ]

        with patch(
            "lexiarbiter.app.ConceptSelectionDialog",
            _FakeConceptDialog,
        ):
            window.set_broader_concept()

        broader_id = annotation.broader_concept_id
        self.assertIsNotNone(broader_id)
        self.assertEqual(
            window.doc.concepts[broader_id],
            "債務不履行責任",
        )
        window.doc.dirty = False
        window.close()

    def test_concept_dialog_distinguishes_existing_target_and_new_broader(self):
        concepts = {
            "C_root": "債之發生原因",
            "C_child": "意定之債",
        }
        broader_by_concept = {
            "C_root": "C_root",
            "C_child": "C_root",
        }

        target_dialog = ConceptSelectionDialog(
            concepts,
            allow_new=False,
            broader_by_concept=broader_by_concept,
        )
        self.assertTrue(target_dialog.input_edit.isHidden())
        root = target_dialog.tree_widget.topLevelItem(0)
        self.assertEqual(root.data(0, Qt.UserRole), "C_root")
        self.assertEqual(root.childCount(), 1)
        child = root.child(0)
        self.assertEqual(child.data(0, Qt.UserRole), "C_child")
        target_dialog._on_tree_item_clicked(child, 0)
        self.assertEqual(
            target_dialog.get_result(),
            ("C_child", None),
        )

        broader_dialog = ConceptSelectionDialog(
            concepts,
            broader_by_concept=broader_by_concept,
        )
        broader_dialog.input_edit.setText("民事責任")
        self.assertEqual(
            broader_dialog.get_result(),
            (None, "民事責任"),
        )

    def test_long_term_replaces_nested_short_terms_at_all_matches(self):
        text = "損害賠償請求權與損害賠償。另有損害賠償請求權。"
        short_term = "損害賠償"
        long_term = "損害賠償請求權"

        short_starts = []
        search_from = 0
        while True:
            start = text.find(short_term, search_from)
            if start == -1:
                break
            short_starts.append(start)
            search_from = start + len(short_term)

        short_annotations = [
            Annotation(
                start=start,
                end=start + len(short_term),
                labels={"entity": "term"},
                concept_id="C_short",
            )
            for start in short_starts
        ]
        doc = Document(
            text=text,
            annotations=short_annotations,
            concepts={
                "C_short": short_term,
                "C_long": long_term,
            },
        )

        source_start = text.find(long_term)
        source_short = next(
            ann for ann in doc.annotations if ann.start == source_start
        )
        doc.remove_annotation(source_short.id)
        source = Annotation(
            start=source_start,
            end=source_start + len(long_term),
            labels={"entity": "term"},
            concept_id="C_long",
        )
        doc.add_annotation(source)

        window = MainWindow()
        window.doc = doc
        window.mode = self.mode

        added, replaced, skipped = window._auto_annotate_same_terms(
            source,
            "entity",
            "term",
        )

        self.assertEqual((added, replaced, skipped), (1, 1, 0))
        long_annotations = [
            ann for ann in doc.annotations if ann.concept_id == "C_long"
        ]
        short_annotations = [
            ann for ann in doc.annotations if ann.concept_id == "C_short"
        ]
        self.assertEqual(len(long_annotations), 2)
        self.assertEqual(len(short_annotations), 1)
        self.assertEqual(
            text[short_annotations[0].start:short_annotations[0].end],
            short_term,
        )
        window.doc.dirty = False
        window.close()

    def test_explanation_button_opens_concept_editor_for_active_occurrence(self):
        text = "意定之債主要指契約。再次提到意定之債。"
        term = "意定之債"
        source = Annotation(
            start=0,
            end=len(term),
            labels={"entity": "term"},
            concept_id="C_term",
        )
        doc = Document(
            text=text,
            annotations=[source],
            concepts={"C_term": term},
        )

        window = MainWindow()
        window.doc = doc
        window.mode = self.mode

        added, replaced, skipped = window._auto_annotate_same_terms(
            source,
            "entity",
            "term",
        )

        self.assertEqual((added, replaced, skipped), (1, 0, 0))
        annotations = sorted(doc.annotations, key=lambda ann: ann.start)
        self.assertEqual(len(annotations), 2)
        for annotation in annotations:
            self.assertEqual(annotation.explanation, "")

        window.editor.attach(doc, self.mode, window.prefs)
        window._active_annotation_id = source.id

        with patch.object(window, "_edit_concept") as edit_concept:
            window.apply_explanation()

        edit_concept.assert_called_once_with("C_term")
        self.assertEqual(source.explanation, "")
        self.assertEqual(annotations[1].explanation, "")

        window.doc.dirty = False
        window.close()

    def test_concept_sidebar_displays_parent_child_tree(self):
        doc = Document(
            text="甲乙",
            concepts={"root": "債之發生原因", "child": "意定之債"},
            broader_concepts={"root": "root", "child": "root"},
        )
        panel = ConceptSidebar()
        panel.refresh(doc)
        root = panel.list_widget.topLevelItem(0)
        self.assertEqual(root.text(0), "債之發生原因")
        self.assertEqual(root.childCount(), 1)
        self.assertEqual(root.child(0).text(0), "意定之債")

    def test_legacy_explanation_range_loads_as_text(self):
        import json
        import tempfile
        from pathlib import Path
        from lexiarbiter.core.io import load_lexa

        payload = {
            "format": "lexa", "version": 1, "schema": "term_definition",
            "source": {"filename": "sample.docx", "meta": {}},
            "concepts": {"C1": "意定之債"},
            "text": "意定之債是契約產生的債。",
            "annotations": [{"start": 0, "end": 4, "labels": {"entity": "term"},
                             "concept_id": "C1", "explanation_start": 5,
                             "explanation_end": 12, "broader_concept_id": "C1"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.lexa"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            doc = load_lexa(path)
        self.assertEqual(doc.annotations[0].explanation, "契約產生的債。")
        self.assertIsNone(doc.annotations[0].explanation_start)
        self.assertEqual(doc.broader_concepts["C1"], "C1")

    def test_official_test_data_loads_in_window_and_roundtrips(self):
        import tempfile
        from pathlib import Path
        from lexiarbiter.core.io import load_lexa, save_lexa

        source = Path(__file__).resolve().parents[1] / "簡易測試資料" / "test_example.lexa"
        window = MainWindow()
        # Keep this roundtrip test independent of autosave files generated by
        # a live app session beside the shared fixture.
        with patch.object(window, "_autosave_companion", return_value=None):
            window.load_file(str(source))
        self.assertEqual(len(window.doc.annotations), 18)
        self.assertEqual(len(window.doc.concepts), 11)
        self.assertEqual(window.concept_panel.list_widget.topLevelItemCount(), 1)
        root = window.concept_panel.list_widget.topLevelItem(0)
        self.assertEqual(root.text(0), "債之發生原因")
        self.assertGreaterEqual(root.childCount(), 2)

        original_explanations = [ann.explanation for ann in window.doc.annotations]
        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory) / "roundtrip.lexa"
            save_lexa(window.doc, saved, self.mode)
            loaded = load_lexa(saved)
        self.assertEqual([ann.explanation for ann in loaded.annotations], original_explanations)
        self.assertEqual(loaded.broader_concepts, window.doc.broader_concepts)
        window.doc.dirty = False
        window.close()

    def test_editing_one_occurrence_keeps_other_explanations_separate(self):
        class _FakeEditDialog:
            PICK_PARENT_FROM_TEXT = 2

            def __init__(self, *_args, **_kwargs):
                pass

            def exec(self):
                return True

            def result_values(self):
                return "意定之債", "C_root", "ann_1", "依當事人合意而成立。"

        first = Annotation(0, 4, {"entity": "term"}, id="ann_1", concept_id="C_child")
        second = Annotation(5, 9, {"entity": "term"}, id="ann_2", concept_id="C_child", explanation="原本解釋")
        doc = Document(
            text="意定之債意定之債",
            annotations=[first, second],
            concepts={"C_root": "債之發生原因", "C_child": "意定之債"},
            broader_concepts={"C_root": "C_root", "C_child": "C_root"},
        )
        window = MainWindow()
        window.doc = doc
        window._active_annotation_id = first.id
        with patch("lexiarbiter.app.ConceptEditDialog", _FakeEditDialog):
            window._edit_concept("C_child")
        self.assertEqual(first.explanation, "依當事人合意而成立。")
        self.assertEqual(second.explanation, "原本解釋")
        self.assertEqual(first.broader_concept_id, "C_root")
        window.doc.dirty = False
        window.close()

    def test_editing_all_checked_occurrences_updates_all_explanations(self):
        first = Annotation(0, 4, {"entity": "term"}, id="ann_1", concept_id="C")
        second = Annotation(5, 9, {"entity": "term"}, id="ann_2", concept_id="C", explanation="舊解釋")
        doc = Document(
            text="概念甲概念乙",
            annotations=[first, second],
            concepts={"C": "概念"},
        )
        window = MainWindow()
        window.doc = doc

        class _FakeEditDialog:
            PICK_PARENT_FROM_TEXT = 2

            def __init__(self, *_args, **_kwargs):
                pass

            def exec(self):
                return True

            def result_values(self):
                return "概念", None, ["ann_1", "ann_2"], "統一解釋"

        with patch("lexiarbiter.app.ConceptEditDialog", _FakeEditDialog):
            window._edit_concept("C")
        self.assertEqual(first.explanation, "統一解釋")
        self.assertEqual(second.explanation, "統一解釋")
        window.doc.dirty = False
        window.close()

    def test_add_child_concept_without_an_annotation(self):
        class _FakeEditDialog:
            PICK_PARENT_FROM_TEXT = 2

            def __init__(self, *_args, **_kwargs):
                pass

            def setWindowTitle(self, _title):
                pass

            def exec(self):
                return True

            def result_values(self):
                return "下位概念", "C_root", None, ""

        window = MainWindow()
        window.doc = Document(
            text="",
            concepts={"C_root": "債之發生原因"},
            broader_concepts={"C_root": "C_root"},
        )
        with patch("lexiarbiter.app.ConceptEditDialog", _FakeEditDialog):
            window._add_concept()
        child_id = next(cid for cid, name in window.doc.concepts.items() if name == "下位概念")
        self.assertEqual(window.doc.broader_concepts[child_id], "C_root")
        self.assertTrue(window.doc.dirty)
        self.assertEqual(window.concept_panel.list_widget.topLevelItemCount(), 1)
        self.assertEqual(window.concept_panel.list_widget.topLevelItem(0).childCount(), 1)
        window.doc.dirty = False
        window.close()

    def test_delete_concept_unlinks_terms_and_reparents_children(self):
        parent = Annotation(0, 1, {"entity": "term"}, id="a", concept_id="C_parent", broader_concept_id="C_parent")
        child = Annotation(1, 2, {"entity": "term"}, id="b", concept_id="C_child", broader_concept_id="C_parent")
        doc = Document(
            text="甲乙",
            annotations=[parent, child],
            concepts={"C_parent": "上位", "C_child": "下位"},
            broader_concepts={"C_parent": "C_parent", "C_child": "C_parent"},
        )
        window = MainWindow()
        window.doc = doc
        with patch("lexiarbiter.app.QMessageBox.question", return_value=QMessageBox.Yes):
            window._delete_concept("C_parent")
        self.assertNotIn("C_parent", doc.concepts)
        self.assertIsNone(parent.concept_id)
        self.assertEqual(doc.broader_concepts["C_child"], "C_child")
        self.assertEqual(child.broader_concept_id, "C_child")
        window.doc.dirty = False
        window.close()

    def test_different_concepts_receive_different_colors(self):
        doc = Document(
            text="甲乙丙",
            concepts={"C_a": "甲", "C_b": "乙"},
        )
        editor = AnnotationEditor()
        editor.attach(doc, self.mode, UserPreferences.load())

        first = Annotation(
            start=0,
            end=1,
            labels={"entity": "term"},
            concept_id="C_a",
        )
        same_concept = Annotation(
            start=1,
            end=2,
            labels={"entity": "term"},
            concept_id="C_a",
        )
        second = Annotation(
            start=2,
            end=3,
            labels={"entity": "term"},
            concept_id="C_b",
        )

        formats = []
        for annotation in (first, same_concept, second):
            fmt = QTextCharFormat()
            editor._apply_format_for_labels(fmt, annotation)
            formats.append(fmt.background().color().name())

        self.assertEqual(formats[0], formats[1])
        self.assertNotEqual(formats[0], formats[2])


if __name__ == "__main__":
    unittest.main()
