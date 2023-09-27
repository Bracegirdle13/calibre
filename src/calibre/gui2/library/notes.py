#!/usr/bin/env python
# License: GPLv3 Copyright: 2023, Kovid Goyal <kovid at kovidgoyal.net>

import os
from functools import partial
from qt.core import (
    QAbstractItemView, QCheckBox, QDialogButtonBox, QFont, QHBoxLayout, QIcon,
    QKeySequence, QLabel, QMenu, QSize, QSplitter, Qt, QTimer, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget, pyqtSignal,
)

from calibre.db.backend import FTSQueryError
from calibre.db.cache import Cache
from calibre.gui2 import Application, error_dialog, gprefs
from calibre.gui2.viewer.widgets import ResultsDelegate, SearchBox
from calibre.gui2.widgets import BusyCursor
from calibre.gui2.widgets2 import Dialog, FlowLayout


def current_db() -> Cache:
    from calibre.gui2.ui import get_gui
    return (getattr(current_db, 'ans', None) or get_gui().current_db).new_api


class NotesResultsDelegate(ResultsDelegate):

    add_ellipsis = False
    emphasize_text = False

    def result_data(self, result):
        if not isinstance(result, dict):
            return None, None, None, None, None
        full_text = result['text']
        parts = full_text.split('\x1d', 2)
        before = after = ''
        if len(parts) > 2:
            before, text = parts[:2]
            after = parts[2].replace('\x1d', '')
        elif len(parts) == 2:
            before, text = parts
        else:
            text = parts[0]
        return False, before, text, after, False


class ResultsList(QTreeWidget):

    current_result_changed = pyqtSignal(object)

    def __init__(self, parent):
        QTreeWidget.__init__(self, parent)
        self.setHeaderHidden(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        self.delegate = NotesResultsDelegate(self)
        self.setItemDelegate(self.delegate)
        self.section_font = QFont(self.font())
        self.itemDoubleClicked.connect(self.item_activated)
        self.section_font.setItalic(True)
        self.currentItemChanged.connect(self.current_item_changed)
        self.number_of_results = 0
        self.item_map = []

    def current_item_changed(self, current, previous):
        if current is not None:
            r = current.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(r, dict):
                self.current_result_changed.emit(r)
        else:
            self.current_result_changed.emit(None)

    def item_activated(self, item):
        r = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(r, dict):
            raise NotImplementedError('TODO: Implement me')

    def show_context_menu(self, pos):
        raise NotImplementedError('TODO: Implement me')

    def show_next(self, backwards=False):
        item = self.currentItem()
        if item is None:
            return
        i = int(item.data(0, Qt.ItemDataRole.UserRole + 1))
        i += -1 if backwards else 1
        i %= self.number_of_results
        self.setCurrentItem(self.item_map[i])

    def edit_note(self, item):
        raise NotImplementedError('TODO: Implement me')

    def keyPressEvent(self, ev):
        if ev.matches(QKeySequence.StandardKey.Delete):
            self.delete_requested.emit()
            ev.accept()
            return
        if ev.key() == Qt.Key.Key_F2:
            item = self.currentItem()
            if item:
                self.edit_note(item)
                ev.accept()
                return
        return QTreeWidget.keyPressEvent(self, ev)

    @property
    def tree_state(self):
        ans = {'closed': set()}
        item = self.currentItem()
        if item is not None:
            ans['current'] = item.data(0, Qt.ItemDataRole.UserRole)
        for item in (self.topLevelItem(i) for i in range(self.topLevelItemCount())):
            if not item.isExpanded():
                ans['closed'].add(item.data(0, Qt.ItemDataRole.UserRole))
        return ans

    @tree_state.setter
    def tree_state(self, state):
        closed = state['closed']
        for item in (self.topLevelItem(i) for i in range(self.topLevelItemCount())):
            if item.data(0, Qt.ItemDataRole.UserRole) in closed:
                item.setExpanded(False)

        cur = state.get('current')
        if cur is not None:
            for item in self.item_map:
                if item.data(0, Qt.ItemDataRole.UserRole) == cur:
                    self.setCurrentItem(item)
                    break

    def set_results(self, results, emphasize_text):
        self.clear()
        self.delegate.emphasize_text = emphasize_text
        self.number_of_results = 0
        self.item_map = []
        db = current_db()
        fm = db.field_metadata
        field_map = {f: {'title': fm[f].get('name') or f, 'matches': []} for f in db.field_supports_notes()}
        for result in results:
            field_map[result['field']]['matches'].append(result)
        for field, entry in field_map.items():
            if not entry['matches']:
                continue
            section = QTreeWidgetItem([entry['title']], 1)
            section.setFlags(Qt.ItemFlag.ItemIsEnabled)
            section.setFont(0, self.section_font)
            section.setData(0, Qt.ItemDataRole.UserRole, field)
            self.addTopLevelItem(section)
            section.setExpanded(True)
            for result in entry['matches']:
                item = QTreeWidgetItem(section, [' '], 2)
                self.item_map.append(item)
                item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemNeverHasChildren)
                item.setData(0, Qt.ItemDataRole.UserRole, result)
                item.setData(0, Qt.ItemDataRole.UserRole + 1, self.number_of_results)
                self.number_of_results += 1
        if self.item_map:
            self.setCurrentItem(self.item_map[0])


class RestrictFields(QWidget):

    restriction_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.l = l = FlowLayout(self)
        l.setContentsMargins(0, 0, 0, 0)
        self.restrict_label = QLabel(_('Restrict to:'))
        self.restricted_fields = []
        self.add_button = b = QToolButton(self)
        b.setToolTip(_('Add categories to which to restrict results.\nWhen no categories are specified no restriction is in effect'))
        b.setIcon(QIcon.ic('plus.png')), b.setText(_('Add')), b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.fields_menu = m = QMenu()
        b.setMenu(m)
        m.aboutToShow.connect(self.build_add_menu)
        self.remove_button = b = QToolButton(self)
        b.setIcon(QIcon.ic('minus.png')), b.setText(_('Remove')), b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.remove_fields_menu = m = QMenu()
        b.setMenu(m)
        m.aboutToShow.connect(self.build_remove_menu)

        db = current_db()
        fm = db.field_metadata
        def field_name(field):
            return fm[field].get('name') or field
        self.field_names = {f:field_name(f) for f in db.field_supports_notes()}
        self.field_labels = {f: QLabel(self.field_names[f], self) for f in sorted(self.field_names, key=self.field_names.get)}
        for l in self.field_labels.values():
            l.setVisible(False)

        self.relayout()

    def relayout(self):
        for i in range(self.l.count()):
            self.l.removeItem(self.l.itemAt(i))
        for l in self.field_labels.values():
            l.setVisible(False)
        self.l.addWidget(self.restrict_label)
        self.l.addWidget(self.add_button)
        for field in self.restricted_fields:
            w = self.field_labels[field]
            w.setVisible(True)
            self.l.addWidget(w)
        self.l.addWidget(self.remove_button)
        self.remove_button.setVisible(bool(self.restricted_fields))

    def build_add_menu(self):
        m = self.fields_menu
        m.clear()
        for field in self.field_labels:
            if field not in self.restricted_fields:
                m.addAction(self.field_names[field], partial(self.add_field, field))

    def build_remove_menu(self):
        m = self.remove_fields_menu
        m.clear()

        for field in self.restricted_fields:
            m.addAction(self.field_names[field], partial(self.remove_field, field))

    def add_field(self, field):
        self.restricted_fields.append(field)
        self.relayout()
        self.restriction_changed.emit()

    def remove_field(self, field):
        self.restricted_fields.remove(field)
        self.relayout()
        self.restriction_changed.emit()


class SearchInput(QWidget):

    show_next_signal = pyqtSignal()
    show_previous_signal = pyqtSignal()
    search_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.l = l = QVBoxLayout(self)
        l.setContentsMargins(0, 0, 0, 0)
        h = QHBoxLayout()
        l.addLayout(h)
        self.search_box = sb = SearchBox(self)
        sb.initialize('library-notes-browser-search-box')
        sb.cleared.connect(self.cleared, type=Qt.ConnectionType.QueuedConnection)
        sb.lineEdit().returnPressed.connect(self.show_next)
        sb.lineEdit().setPlaceholderText(_('Enter words to search for'))
        h.addWidget(sb)

        self.next_button = nb = QToolButton(self)
        h.addWidget(nb)
        nb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        nb.setIcon(QIcon.ic('arrow-down.png'))
        nb.clicked.connect(self.show_next)
        nb.setToolTip(_('Find next match'))

        self.prev_button = nb = QToolButton(self)
        h.addWidget(nb)
        nb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        nb.setIcon(QIcon.ic('arrow-up.png'))
        nb.clicked.connect(self.show_previous)
        nb.setToolTip(_('Find previous match'))

        self.restrict = r = RestrictFields(self)
        r.restriction_changed.connect(self.search_changed)
        l.addWidget(r)


    @property
    def current_query(self):
        return {
            'fts_engine_query': self.search_box.lineEdit().text().strip(),
            'restrict_to_fields': tuple(self.restrict.restricted_fields),
            'use_stemming': bool(self.parent().use_stemmer.isChecked()),
        }

    def cleared(self):
        self.search_changed.emit()

    def show_next(self):
        self.show_next_signal.emit()

    def show_previous(self):
        self.show_previous_signal.emit()


class NotesBrowser(Dialog):

    current_query = None

    def __init__(self, parent=None):
        super().__init__(_('Browse notes'), 'browse-notes-dialog', default_buttons=QDialogButtonBox.StandardButton.Close)
        self.setWindowIcon(QIcon.ic('notes.png'))

    def sizeHint(self):
        return QSize(900, 600)

    def setup_ui(self):
        self.l = l = QVBoxLayout(self)

        self.search_input = si = SearchInput(self)
        si.search_changed.connect(self.search_changed)
        l.addWidget(si)

        self.splitter = s = QSplitter(self)
        l.addWidget(s, stretch=100)
        s.setChildrenCollapsible(False)

        self.results_list = rl = ResultsList(self)
        si.show_next_signal.connect(rl.show_next)
        si.show_previous_signal.connect(partial(rl.show_next, backwards=True))
        s.addWidget(rl)

        self.use_stemmer = us = QCheckBox(_('&Match on related words'))
        us.setChecked(gprefs['browse_notes_use_stemmer'])
        us.setToolTip('<p>' + _(
            'With this option searching for words will also match on any related words (supported in several languages). For'
            ' example, in the English language: <i>correction</i> matches <i>correcting</i> and <i>corrected</i> as well'))
        us.stateChanged.connect(lambda state: gprefs.set('browse_notes_use_stemmer', state != Qt.CheckState.Unchecked.value))

        h = QHBoxLayout()
        l.addLayout(h)
        h.addWidget(us), h.addStretch(10), h.addWidget(self.bb)
        QTimer.singleShot(0, self.do_find)

    def search_changed(self):
        if self.search_input.current_query != self.current_query:
            self.do_find()

    def do_find(self, backwards=False):
        q = self.search_input.current_query
        if q == self.current_query:
            self.results_list.show_next(backwards)
            return
        try:
            with BusyCursor():
                results = current_db().search_notes(
                    highlight_start='\x1d', highlight_end='\x1d', snippet_size=64, **q
                )
                self.results_list.set_results(results, bool(q['fts_engine_query']))
                self.current_query = q
        except FTSQueryError as err:
            return error_dialog(self, _('Invalid search expression'), '<p>' + _(
                'The search expression: {0} is invalid. The search syntax used is the'
                ' SQLite Full text Search Query syntax, <a href="{1}">described here</a>.').format(
                    err.query, 'https://www.sqlite.org/fts5.html#full_text_query_syntax'),
                det_msg=str(err), show=True)


if __name__ == '__main__':
    from calibre.library import db
    app = Application([])
    current_db.ans = db(os.path.expanduser('~/test library'))
    br = NotesBrowser()
    br.exec()
    del br
    del app