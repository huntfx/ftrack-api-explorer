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


def entityRepr(entityType, entityID=None):
    """Create a correct representation of an entity.
    >>> project = session.query('Project').first()
    >>> entityRepr(project)
    Project(id='12345678')
    >>> entityRepr(session.types['Project'], '12345678')
    Project(id='12345678')
    """
    if entityID is None:
        entity, entityType = entityType, type(entityType)

    primaryKeys = entityType.primary_key_attributes

    if entityID is None:
        entityID = [entity[k] for k in primaryKeys]
    elif not isinstance(entityID, (list, tuple)):
        entityID = [entityID]

    args = ', '.join(f'{k}={v!r}' for k, v in zip(primaryKeys, entityID))
    return f'{entityType.entity_type}({args})'


def isKeyLoaded(entity, key):
    """Determine if an entity has a key loaded."""
    attrStorage = getattr(entity, '_ftrack_attribute_storage')
    if attrStorage is None or key not in attrStorage:
        return False
    return attrStorage[key]['remote'] != ftrack_api.symbol.NOT_SET


class EntityCache(object):
    """Cache entity values."""

    __slots__ = ('id',)
    Cache = defaultdict(dict)
    Entities = {}
    Types = {}

    def __init__(self, entity):
        self.id = entityRepr(entity)
        self.Entities[self.id] = entity

    def __getitem__(self, key):
        return self.cache[key]

    def __setitem__(self, key, value):
        self.cache[key] = value

    def __contains__(self, key):
        return key in self.cache

    @property
    def cache(self):
        return self.Cache[self.id]

    @classmethod
    def reset(cls):
        """Remove all cache."""
        cls.Cache = defaultdict(dict)

    @classmethod
    def load(cls, entity):
        """Add an entity to cache."""
        cache = cls(entity)
        attributes = type(entity).attributes
        for key in entity.keys():
            if not isKeyLoaded(entity, key):
                continue

            cache[key] = entity[key]
            attr = attributes.get(key)
            if isinstance(attr, ftrack_api.attribute.ReferenceAttribute):
                cls.load(entity[key])
            elif isinstance(attr, ftrack_api.attribute.CollectionAttribute):
                for child in entity[key]:
                    cls.load(child)

    @classmethod
    def types(cls, session=None):
        """Cache the entity types to avoid opening more sessions."""
        if not cls.Types:
            print('Loading FTrack entity types...')
            if session is not None:
                cls.Types = session.types
            else:
                with ftrack_api.Session() as session:
                    cls.Types = session.types
                    return dict(cls.Types)
        return dict(cls.Types)

    @classmethod
    def entity(cls, name):
        """Get an entity from its name or return None."""
        return cls.Entities.get(name)


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
    AutoPopulateRole = QtCore.Qt.UserRole + 5

    topLevelEntityAdded = QtCore.Signal()

    @ftrack_session
    def __init__(self, parent=None, session=None, **kwargs):
        super().__init__(parent=parent, **kwargs)
        self.setWindowPalette('Nuke', 12)

        # Build menu
        options = self.menuBar().addMenu('Options')
        self._autoPopulate = QtWidgets.QAction('Enable auto-population')
        self._autoPopulate.setCheckable(True)
        self._autoPopulate.setChecked(True)
        options.addAction(self._autoPopulate)

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
        self._queryText.setupCompleter(sorted(EntityCache.types(session=session)))
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

    def autoPopulate(self):
        """Determine if auto population is allowed."""
        return self._autoPopulate.isChecked()

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
                    self._loadEntity(entity)
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
                self._loadEntity(entity)

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

            # Load the remaining entity keys if required
            if not model.data(index, self.AutoPopulateRole) and self.autoPopulate():
                parentType = model.data(index, self.EntityTypeRole)
                parentPrimaryKeys = model.data(index, self.EntityPrimaryKeyRole).split(';')
                childKey = model.data(index, self.EntityKeyRole)
                item = model.itemFromIndex(index)
                loaded = [item.child(row).text() for row in range(item.rowCount())]
                self.loadEntity(parentType, parentPrimaryKeys, key=childKey, parent=item, _loaded=loaded)
                model.setData(index, True, self.AutoPopulateRole)

        # Mark the item as visited
        elif model.data(index, self.DummyRole) is not None:
            model.setData(index, True, self.VisitRole)
            model.setData(index, self.autoPopulate(), self.AutoPopulateRole)
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
    def loadEntity(self, entityType, entityID, key=None, parent=None, _loaded=None):
        """Wrap the load function to allow multiple entities to be added."""
        session = None

        # Only start a session if not loading cached data
        if self.autoPopulate():
            session = ftrack_api.Session()

            # Build a list of potential entities
            if entityID:
                entity = session.get(entityType, entityID)
                if entity is None:
                    print(f'Could not find entity.')
                    entities = []
                else:
                    entities = [entityID]
            else:
                entities = session.query(entityType)

            # Load anything not yet loaded
            for i, entity in enumerate(entities):
                if not isinstance(entity, ftrack_api.entity.base.Entity):
                    entities[i] = session.get(entityType, entityID)

        # Load entity from cache
        else:
            name = entityRepr(EntityCache.types()[entityType], entityID)
            entity = EntityCache.entity(name)
            if entity is not None:
                entities = [entity]

        # Add each entity to the GUI
        for entity in entities:
            try:
                self._loadEntity(entity, key=key, parent=parent, _loaded=_loaded)
            # The GUI has likely refreshed so we can stop the query here
            except RuntimeError:
                break

        if session is not None:
            session.close()

    def _loadEntity(self, entity, key=None, parent=None, _loaded=None):
        """Add a new FTrack entity.
        Optionally set key to load a child entity.
        """
        if _loaded is None:
            _loaded = []
        else:
            _loaded = list(sorted(_loaded))

        name = entityRepr(entity)
        cache = EntityCache(entity)
        attributes = type(entity).attributes

        # Add a new top level item
        if parent is None:
            root = self._entityData.model().invisibleRootItem()
            parent = self.addItem(root, None, entity, entity)
            self.topLevelEntityAdded.emit()
            print(f'Found {name}')
            EntityCache.load(entity)

            # Stop here as we don't want to force load everything
            return

        if key:
            print(f'Loading data for {key!r}...')
        else:
            print(f'Loading data for {name}...')

        # Allow individual keys to be loaded
        if key:
            value = entity[key]
            attr = attributes.get(key)
            if isinstance(attr, ftrack_api.attribute.ReferenceAttribute):
                entity = value

            if isinstance(attr, ftrack_api.attribute.CollectionAttribute):
                for v in value:
                    self.addItem(parent, None, v, v)
                print(f'Finished loading {key!r} collection')
                return

            if isinstance(attr, ftrack_api.attribute.KeyValueMappedCollectionAttribute):
                for k, v in sorted(value.items()):
                    self.addItem(parent, k, v, v)
                print(f'Finished loading {key!r} collection')
                return

        # Load all keys
        keys = set(entity.keys())

        # I don't like to hardcode things, but project['descendants'] is slow as fuck
        # I'm disabling it for safety as it temporarily brought down the server
        if type(entity).entity_type == 'Project':
            keys.remove('descendants')

        # Load a new entity
        for key in sorted(keys):
            if key in _loaded:
                continue

            # Load cached value
            if key in cache:
                print(f'Found {key!r} in cache...')
                value = cache[key]

            # Fetch from server
            elif self.autoPopulate():
                print(f'Reading {key!r}...')
                try:
                    value = entity[key]
                except ftrack_api.exception.ServerError:
                    print(f'Failed to read {key!r}')
                    continue
                else:
                    cache[key] = value
            else:
                continue

            # Insert in alphabetical order
            row = None
            if _loaded:
                for i, k in enumerate(_loaded):
                    if k > key:
                        row = i
                        _loaded.insert(i, key)
                        break

            self.addItem(parent, key, value, entity, row=row)
        print(f'Finished reading data from {name}')

    def appendRow(self, parent, entityKey, entityValue='', entityType='', row=None):
        """Create a new row of QStandardItems."""
        item = QtGui.QStandardItem(entityKey)
        data = (item, QtGui.QStandardItem(entityValue), QtGui.QStandardItem(entityType))
        if row is None:
            parent.appendRow(data)
        else:
            parent.insertRow(row, data)
        return item

    def addItem(self, parent, key, value, entity, row=None):
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
            child = self.appendRow(parent, key, '', className, row=row)
            for i, v in enumerate(value):
                k = str(i)
                self.addItem(child, k, v, entity)

        elif isinstance(value, dict):
            child = self.appendRow(parent, key, '', className, row=row)
            for k, v in sorted(value.items()):
                self.addItem(child, k, v, entity)

        elif isinstance(value, ftrack_api.entity.base.Entity):
            entityStr = entityRepr(value)
            if key is None:
                key, entityStr = entityStr, ''
            child = self.appendRow(parent, key, entityStr, type(value).entity_type, row=row)
            self.addDummyItem(child, value, '')

        elif isinstance(value, ftrack_api.collection.Collection):
            child = self.appendRow(parent, key, '', className, row=row)
            self.addDummyItem(child, entity, key)

        elif isinstance(value, ftrack_api.collection.KeyValueMappedCollectionProxy):
            child = self.appendRow(parent, key, '', className, row=row)
            self.addDummyItem(child, entity, key)

        else:
            child = self.appendRow(parent, key, str(value), className, row=row)
        return child

    def addDummyItem(self, parent, entity, key):
        """Create a dummy item for things not yet loaded."""
        model = self._entityData.model()

        # Store data about the parent entities
        primary_key_attributes = type(entity).primary_key_attributes
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
