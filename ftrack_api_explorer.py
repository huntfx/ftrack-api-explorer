from collections import defaultdict
from functools import wraps
from threading import Thread

import ftrack_api
from Qt import QtCore, QtGui, QtWidgets
from vfxwindow import VFXWindow


def ftrack_session(func):
    """Wrap a function in this to ensure a session is created.
    It requires the session to be a keyword argument.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        if kwargs.get('session'):
            return func(*args, **kwargs)
        with ftrack_api.Session() as kwargs['session']:
            return func(*args, **kwargs)
    return wrapper


def deferred(func):
    """Run a function in a thread."""
    def wrapper(*args, **kwargs):
        thread = Thread(target=func, args=args, kwargs=kwargs)
        thread.daemon = True
        thread.start()
        return thread
    return wrapper


@ftrack_session
def entityRepr(entity, session=None):
    """Create a correct representation of an entity.
    >>> project = session.query('Project').first()
    >>> entityRepr(project)
    Project(id='12345678')
    """
    primaryKeys = session.types[entity.__class__.__name__].primary_key_attributes
    keys = [entity[k] for k in primaryKeys]
    args = ', '.join(f'{k}={v!r}' for k, v in zip(primaryKeys, keys))
    return f'{entity.__class__.__name__}({args})'


def isKeyLoaded(entity, key):
    """Determine if a key already contains a value."""
    attrStorage = getattr(entity, '_ftrack_attribute_storage')
    if attrStorage is None or key not in attrStorage:
        return False
    return attrStorage[key]['remote'] != ftrack_api.symbol.NOT_SET


class EntityCache(object):
    """Cache any entity values."""

    __slots__ = ('id', 'cache')
    Cache = defaultdict(dict)

    def __init__(self, entityID):
        self.id = entityID
        self.cache = self.Cache[entityID]

    def __getitem__(self, key):
        return self.cache[key]

    def __setitem__(self, key, value):
        self.cache[key] = value

    def __contains__(self, key):
        return key in self.cache

    @classmethod
    def reset(cls):
        """Remove all cache."""
        cls.Cache = defaultdict(dict)

    @classmethod
    def load(cls, entity):
        """Add an entity to cache."""
        cache = cls(entity['id'])
        for key in entity.keys():
            if isKeyLoaded(entity, key):
                cache[key] = entity[key]
                if isinstance(entity[key], ftrack_api.collection.Collection):
                    for collection in entity[key]:
                        cls.addEntity(collection)


class QueryEdit(QtWidgets.QLineEdit):
    """Add a few features to the line edit widget."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setPlaceholderText('Type custom query here...')

    def setupCompleter(self, stringList):
        completer = QtWidgets.QCompleter()
        completer.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
        self.setCompleter(completer)
        model = QtCore.QStringListModel()
        completer.setModel(model)
        model.setStringList(stringList)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self.completer().complete()


class FTrackExplorer(VFXWindow):
    WindowID = 'ftrack-api-explorer'
    WindowName = 'FTrack API Explorer'

    VisitRole = QtCore.Qt.UserRole
    DummyRole = QtCore.Qt.UserRole + 1
    EntityPrimaryKeyRole = QtCore.Qt.UserRole + 2
    EntityTypeRole = QtCore.Qt.UserRole + 3
    EntityKeyRole = QtCore.Qt.UserRole + 4

    topLevelEntityAdded = QtCore.Signal()

    @ftrack_session
    def __init__(self, parent=None, session=None, **kwargs):
        super().__init__(parent=parent, **kwargs)
        self.setWindowPalette('Nuke', 12)

        # Build layout
        layout = QtWidgets.QVBoxLayout()
        widget = QtWidgets.QWidget()
        widget.setLayout(layout)
        self.setCentralWidget(widget)

        queryLayout = QtWidgets.QHBoxLayout()
        layout.addLayout(queryLayout)
        queryLabel = QtWidgets.QLabel('Query:')
        queryLayout.addWidget(queryLabel)
        self._queryText = QueryEdit()
        self._queryText.setupCompleter(sorted(session.types))
        queryLayout.addWidget(self._queryText)
        queryFirst = QtWidgets.QPushButton('Get First')
        queryLayout.addWidget(queryFirst)
        queryAll = QtWidgets.QPushButton('Get All')
        queryLayout.addWidget(queryAll)

        self._entityData = QtWidgets.QTreeView()
        layout.addWidget(self._entityData)
        entityDataModel = QtGui.QStandardItemModel()
        entityDataModel.setHorizontalHeaderLabels(('Key', 'Value', 'Type'))
        self._entityData.setModel(entityDataModel)

        footer = QtWidgets.QHBoxLayout()
        layout.addLayout(footer)
        footer.addStretch()
        clear = QtWidgets.QPushButton('Clear')
        footer.addWidget(clear)
        footer.addStretch()

        # Signals
        self._entityData.expanded.connect(self.populateChildren)
        clear.clicked.connect(self.clear)
        self.topLevelEntityAdded.connect(self.autoResizeColumns)
        queryAll.clicked.connect(self.executeAll)
        queryFirst.clicked.connect(self.executeFirst)
        self._queryText.returnPressed.connect(self.executeAll)

    @QtCore.Slot()
    def executeAll(self):
        """Get all the results of the query."""
        query = self._queryText.text()
        if not query:
            return

        print(f'Executing {query!r}...')
        with ftrack_api.Session() as session:
            try:
                for entity in session.query(query):
                    self._loadEntity(entity, session=session)
            except KeyError:
                print(f'Invalid query: {query!r}')
                return

    @QtCore.Slot()
    def executeFirst(self):
        """Get the first result of the query."""
        query = self._queryText.text()
        if not query:
            return

        print(f'Executing {query!r}...')
        with ftrack_api.Session() as session:
            try:
                entity = session.query(query).first()
            except KeyError:
                print(f'Invalid query: {query!r}')
                return
            if entity is not None:
                self._loadEntity(entity, session=session)

    @QtCore.Slot()
    def entityTypeChanged(self):
        """Reset the Type ID text."""
        self._typeID.setText('')

    @QtCore.Slot()
    def clear(self):
        """Remove all the data."""
        self._entityData.model().removeRows(0, self._entityData.model().rowCount())
        EntityCache.reset()

    @QtCore.Slot(QtCore.QModelIndex)
    def populateChildren(self, index=None):
        """Load all child items when an entity is expanded."""
        model = self._entityData.model()

        # Check if the items have already been populated
        if model.data(index, self.VisitRole) is not None:
            return

        # Mark the item as visited
        if model.data(index, self.DummyRole) is not None:
            model.setData(index, True, self.VisitRole)
            item = model.itemFromIndex(index)

            # Remove the dummy item
            model.removeRow(0, index)

            # Populate with entities
            parentType = model.data(index, self.EntityTypeRole)
            parentPrimaryKeys = model.data(index, self.EntityPrimaryKeyRole).split(';')
            childKey = model.data(index, self.EntityKeyRole)
            self.loadEntity(parentType, parentPrimaryKeys, key=childKey, parent=item)

    @QtCore.Slot()
    def autoResizeColumns(self):
        """Resize the columns to fit the contents.
        This can only be called outside of a thread, otherwise this appears:
        QBasicTimer::start: QBasicTimer can only be used with threads started with QThread
        """
        self._entityData.resizeColumnToContents(0)
        self._entityData.setColumnWidth(1, self._entityData.columnWidth(0))
        self._entityData.resizeColumnToContents(2)
        try:
            self.topLevelEntityAdded.disconnect(self.autoResizeColumns)
        except RuntimeError:
            pass

    @deferred
    @ftrack_session
    def loadEntity(self, entityType, entityID, key=None, parent=None, session=None):
        """Wrap the load function to allow multiple entities to be added."""
        # Build a list of potential entities
        if entityID:
            entity = session.get(entityType, entityID)
            if entity is None:
                print(f'Could not find entity.')
                return
            entities = [entityID]
        else:
            entities = session.query(entityType)

        # Add each entity to the GUI
        for entity in entities:
            if not isinstance(entity, ftrack_api.entity.base.Entity):
                entity = session.get(entityType, entityID)
                if entity is None:
                    print(f'Could not find entity.')
                    continue

            try:
                self._loadEntity(entity, key=key, parent=parent, session=session)
            # The GUI has likely refreshed so we can stop the query here
            except RuntimeError:
                return

    @ftrack_session
    def _loadEntity(self, entity, key=None, parent=None, session=None):
        """Add a new FTrack entity.
        Optionally set key to load a child entity.
        """
        entityStr = entityRepr(entity, session=session)
        entityCache = EntityCache(entity['id'])

        # Add a new top level item
        if parent is None:
            root = self._entityData.model().invisibleRootItem()
            parent = self.addItem(root, None, entity, entity, session=session)
            self.topLevelEntityAdded.emit()
            print(f'Found {entityStr}')
            EntityCache.load(entity)

            # Stop here as we don't want to force load everything
            return

        if key:
            print(f'Loading data for {key!r}...')
        else:
            print(f'Loading data for {entityStr}...')

        # Allow individual keys to be loaded
        if key:
            value = entity[key]
            if isinstance(value, ftrack_api.entity.base.Entity):
                entity = value

            elif isinstance(value, ftrack_api.collection.Collection):
                for i, v in enumerate(value):
                    self.addItem(parent, None, v, v, session=session)
                print(f'Finished loading {key!r} collection')
                return

            elif isinstance(value, ftrack_api.collection.KeyValueMappedCollectionProxy):
                for k, v in sorted(value.items()):
                    self.addItem(parent, k, v, v, session=session)
                print(f'Finished loading {key!r} collection')
                return

        # Load all keys
        keys = set(entity.keys())

        # I don't like to hardcode things, but project['descendants'] is slow as fuck
        # I'm disabling it for safety as it temporarily brought down the server
        if isinstance(entity, session.types['Project']):
            keys.remove('descendants')

        # Load a new entity
        for key in sorted(keys):
            if key in entityCache:
                print(f'Found {key!r} in cache...')
                value = entityCache[key]
            else:
                print(f'Reading {key!r}...')
                try:
                    value = entity[key]
                except ftrack_api.exception.ServerError:
                    print(f'Failed to read {key!r}')
                    continue
                else:
                    entityCache[key] = value
            self.addItem(parent, key, value, entity, session=session)
        print(f'Finished reading data from {entityStr}')

    def appendRow(self, parent, entityKey, entityValue='', entityType=''):
        """Create a new row of QStandardItems."""
        item = QtGui.QStandardItem(entityKey)
        parent.appendRow((item, QtGui.QStandardItem(entityValue), QtGui.QStandardItem(entityType)))
        return item

    @ftrack_session
    def addItem(self, parent, key, value, entity, session=None):
        """Add an FTrack entity value.

        Parameters:
            parent (QStandardItem): Parent item to append to.
            key (str): The key used to access the current entity.
            value (object): Value belonging to entity['key'].
            entity (Entity): Parent entity.
                This is used with the dummy items so that the child
                entity can easily be queried later.
        """
        className = value.__class__.__name__

        if isinstance(value, (list, tuple)):
            child = self.appendRow(parent, key, '', className)
            for i, v in enumerate(value):
                k = str(i)
                self.addItem(child, k, v, entity, session=session)

        elif isinstance(value, dict):
            child = self.appendRow(parent, key, '', className)
            for k, v in sorted(value.items()):
                self.addItem(child, k, v, entity, session=session)

        elif isinstance(value, ftrack_api.entity.base.Entity):
            entityStr = entityRepr(value, session=session)
            if key is None:
                key, entityStr = entityStr, ''
            child = self.appendRow(parent, key, entityStr, value.__class__.__name__)
            self.addDummyItem(child, value, '', session=session)

        elif isinstance(value, ftrack_api.collection.Collection):
            child = self.appendRow(parent, key, '', className)
            self.addDummyItem(child, entity, key, session=session)

        elif isinstance(value, ftrack_api.collection.KeyValueMappedCollectionProxy):
            child = self.appendRow(parent, key, '', className)
            self.addDummyItem(child, entity, key, session=session)

        else:
            child = self.appendRow(parent, key, str(value), className)
        return child

    def addDummyItem(self, parent, entity, key, session=None):
        """Create a dummy item for things not yet loaded."""
        model = self._entityData.model()

        # Store data about the parent entities
        primary_key_attributes = session.types[entity.__class__.__name__].primary_key_attributes
        parentIndex = model.indexFromItem(parent)
        model.setData(parentIndex, True, self.DummyRole)
        model.setData(parentIndex, str(key), self.EntityKeyRole)
        model.setData(parentIndex, str(entity.__class__.__name__), self.EntityTypeRole)
        model.setData(parentIndex, ';'.join(entity[k] for k in map(str, primary_key_attributes)), self.EntityPrimaryKeyRole)

        # Create the dummy item
        item = QtGui.QStandardItem('<not loaded>')
        parent.appendRow(item)
        return item


if __name__ == '__main__':
    FTrackExplorer.show()