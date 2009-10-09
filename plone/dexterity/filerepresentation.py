import tempfile

# capitalised for Python 2.4 compatibility
from email.Generator import Generator
from email.Parser import FeedParser
from email.Message import Message

from rwproperty import getproperty, setproperty

from zope.interface import implements
from zope.component import adapts, getUtility
from zope.schema import getFieldsInOrder
from zope.event import notify
from zope.lifecycleevent import modified, ObjectCreatedEvent

from zope.component.interfaces import IFactory
from zope.interface.interfaces import IInterface
from zope.size.interfaces import ISized

from Acquisition import aq_base, Implicit
from zExceptions import Unauthorized, MethodNotAllowed
from webdav.Resource import Resource
from ZPublisher.Iterators import IStreamIterator
from Products.CMFCore.utils import getToolByName

from zope.filerepresentation.interfaces import IRawReadFile
from zope.filerepresentation.interfaces import IRawWriteFile

from zope.filerepresentation.interfaces import IDirectoryFactory
from zope.filerepresentation.interfaces import IFileFactory

from plone.rfc822.interfaces import IPrimaryField
from plone.rfc822 import constructMessageFromSchemata
from plone.rfc822 import initializeObjectFromSchemata

from plone.dexterity.interfaces import IDexterityContent
from plone.dexterity.interfaces import IDexterityContainer

from plone.dexterity.interfaces import DAV_FOLDER_DATA_ID

from plone.dexterity.utils import iterSchemata
from plone.memoize.instance import memoize

class DAVResourceMixin(object):
    """Mixin class for WebDAV resource support.
    
    The main purpose of this class is to implement the Zope 2 WebDAV API to
    delegate to more granular adapters.
    """
    
    def get_size(self):
        """Get the size of the content item in bytes. Used both in folder
        listings and in DAV PROPFIND requests.
        
        The default implementation delegates to an ISized adapter and calls
        getSizeForSorting(). This returns a tuple (unit, value). If the unit
        is 'bytes', the value is returned, otherwise the size is 0.
        """
        sized = ISized(self, None)
        if sized is None:
            return 0
        unit, size = sized.sizeForSorting()
        if unit == 'bytes':
            return size
        return 0
    
    def content_type(self):
        """Return the content type (MIME type) of the tiem
        """
        readFile = IRawReadFile(self, None)
        if readFile is None:
            return None
        return readFile.mimeType
    
    def Format(self):
        """Return the content type (MIME type) of the item
        """
        return self.content_type()
    
    def manage_DAVget(self):
        """Get the body of the content item in a WebDAV response.
        """
        return self.manage_FTPget()
    
    def manage_FTPget(self, REQUEST=None, RESPONSE=None):
        """Return the body of the content item in an FTP or WebDAV response.
        
        This adapts self to IRawReadFile(), which is then returned as an
        iterator. The adapter should provide IStreamIterator.
        """
        reader = IRawReadFile(self, None)
        if reader is None:
            return ''
        
        request = REQUEST is not None and REQUEST or self.REQUEST
        response = RESPONSE is not None and RESPONSE or request.response
        
        mimeType = reader.mimeType
        encoding = reader.encoding
        
        if mimeType is not None:
            if encoding is not None:
                response.setHeader('Content-Type', '%s; charset="%s"' % (mimeType, encoding,))
            else:
                response.setHeader('Content-Type', mimeType)
        
        size = reader.size()
        if size is not None:
            response.setHeader('Content-Length', str(size))
        
        # if the reader is an iterator that the publisher can handle, return
        # it as-is. Otherwise, read the full contents
        
        if ((IInterface.providedBy(IStreamIterator) and IStreamIterator.providedBy(reader))
         or (not IInterface.providedBy(IStreamIterator) and IStreamIterator.isImplementedBy(reader))
        ):
            return reader
        else:
            return reader.read()
    
    def PUT(self, REQUEST=None, RESPONSE=None):
        """WebDAV method to replace self with a new resource. This is also
        used when initialising an object just created from a NullResource.
        
        This will look up an IRawWriteFile adapter on self and write to it,
        line-by-line, from the request body.
        """
        request = REQUEST is not None and REQUEST or self.REQUEST
        response = RESPONSE is not None and RESPONSE or request.response
        
        self.dav__init(request, response)
        self.dav__simpleifhandler(request, response, refresh=1)
        
        infile = request.get('BODYFILE', None)
        if infile is None:
            raise MethodNotAllowed("Cannot complete PUT request: No BODYFILE in request")
        
        writer = IRawWriteFile(self, None)
        if writer is None:
            raise MethodNotAllowed("Cannot complete PUT request: No IRawWriteFile adapter found")
        
        contentTypeHeader = request.get_header('content-type', None)
        
        if contentTypeHeader is not None:
            msg = Message()
            msg['Content-Type'] = contentTypeHeader
            
            mimeType = msg.get_content_type()
            if mimeType is not None:
                writer.mimeType = mimeType
            
            charset = msg.get_param('charset')
            if charset is not None:
                writer.encoding = charset
        
        try:
            for chunk in infile:
                writer.write(chunk)
        finally:
            writer.close()
        
        modified(self)
        return response


class DAVCollectionMixin(DAVResourceMixin):
    """Mixin class for WebDAV collection support.
    
    The main purpose of this class is to implement the Zope 2 WebDAV API to
    delegate to more granular adapters.
    """
    
    def MKCOL_handler(self, id, REQUEST=None, RESPONSE=None):
        """Handle "make collection" by delegating to an IDirectoryFactory
        adapter.
        """
        factory = IDirectoryFactory(self, None)
        if factory is None:
            raise MethodNotAllowed("Cannot create collection: No IDirectoryFactory adapter found")
        factory(id)
    
    def PUT_factory(self, name, contentType, body):
        """Handle constructing a new object upon a PUT request by delegating
        to an IFileFactory adapter
        """
        factory = IFileFactory(self, None)
        if factory is None:
            return None
        return factory(name, contentType, body)
    
    def listDAVObjects(self):
        """Return objects for WebDAV folder listings.
        
        We add a non-folderish pseudo object which contains the "body" data
        for this container.
        """
        parentList = super(DAVCollectionMixin, self).listDAVObjects()
        if not parentList:
            parentList = []
        else:
            parentList = list(parentList)
        
        # insert the FolderDataResource pseudo child
        faux = FolderDataResource(DAV_FOLDER_DATA_ID, self).__of__(self)
        parentList.insert(0, faux)
        return parentList


class FolderDataResource(Implicit, Resource):
    """This object is a proxy which is created on-demand during traversal,
    to allow access to the "file-like" aspects of a container type.
    
    When a Container object is listed via WebDAV, the first item in the folder
    listing is an instance of this class with an id of '_data'. When
    requested, the default Dexterity IPublishTraverse adapter will also return
    an instance (the instances are non-persistent). A GET, PUT, HEAD, LOCK,
    UNLOCK, PROPFIND or PROPPATCH request against this resource will be 
    treated as if it were a request against the parent object, treating it
    as a resource (file) rather than a collection (folder).
    """

    __dav_collection__ = 0
    
    def __init__(self, name, parent):
        self.__dict__.update({'__parent__': parent, '__name__': name})
    
    # We need to proxy certain things to the parent for getting and setting
    # of property sheet values to work.
    # 
    # XXX: A better approach may be to define a custom PropertySheets type
    # with some kind of wrapping property sheet that redefines v_self() to
    # be the container.
    
    def __getattr__(self, name):
        """Fall back on parent for certain things, even if we're aq_base'd.
        This makes propertysheet access work.
        """
        if hasattr(self.__parent__.aq_base, name):
            return getattr(self.__parent__, name)
        raise AttributeError(name)
    
    def __setattr__(self, name, value):
        """Set certain attributes on the parent
        """
        if name in self.__dict__:
            object.__setattr__(self, name, value)
        elif self.__parent__.hasProperty(name):
            setattr(self.__parent__, name, value)
        else:
            object.__setattr__(self, name, value)
    
    @getproperty
    def _properties(self):
        return self.__parent__._properties
    @setproperty
    def _properties(self, value):
        self.__parent__._properties = value
    
    @property
    def id(self):
        return self.__name__
    
    def getId(self):
        """Get id for traveral purposes
        """
        return self.__name__
        
    def HEAD(self, REQUEST, RESPONSE):
        """HEAD request: use the Resource algorithm on the data of the
        parent.
        """
        return Resource.HEAD(self.__parent__, REQUEST, RESPONSE)
    
    def OPTIONS(self, REQUEST, RESPONSE):
        """OPTIONS request: delegate to parent
        """
        return self.__parent__.OPTIONS(REQUEST, RESPONSE)
    
    def TRACE(self, REQUEST, RESPONSE):
        """TRACE request: delegate to parent
        """
        return self.__parent__.TRACE(REQUEST, RESPONSE)
    
    def PROPFIND(self, REQUEST, RESPONSE):
        """PROPFIND request: use Resource algorithm on self, so that we do
        not appear as a folder.
        
        Certain things may be acquired, notably .propertysheets
        """
        return super(FolderDataResource, self).PROPFIND(REQUEST, RESPONSE)
    
    def PROPPATCH(self, REQUEST, RESPONSE):
        """PROPPATCH request: Use Resource algorithm on self, so that we do
        not appear as a folder.
        
        Certain things may be acquired, notably .propertysheets
        """
        return super(FolderDataResource, self).PROPPATCH(REQUEST, RESPONSE)
    
    def LOCK(self, REQUEST, RESPONSE):
        """LOCK request: delegate to parent
        """
        return self.__parent__.LOCK(REQUEST, RESPONSE)
    
    def UNLOCK(self, REQUEST, RESPONSE):
        """UNLOCK request: delegate to parent
        """
        return self.__parent__.UNLOCK(REQUEST, RESPONSE)
    
    def PUT(self, REQUEST, RESPONSE):
        """PUT request: delegate to parent
        """
        return self.__parent__.PUT(REQUEST, RESPONSE)
    
    def MKCOL(self, REQUEST, RESPONSE):
        """MKCOL request: not allowed
        """
        raise MethodNotAllowed('Cannot create a collection inside a folder data: try at the folder level instead')
    
    def DELETE(self, REQUEST, RESPONSE):
        """DELETE request: not allowed
        """
        raise MethodNotAllowed('Cannot delete folder data: delete folder instead')
    
    def COPY(self, REQUEST, RESPONSE):
        """COPY request: not allowed
        """
        raise MethodNotAllowed('Cannot copy folder data: copy the folder instead')
        
    def MOVE(self, REQUEST, RESPONSE):
        """MOVE request: not allowed
        """
        raise MethodNotAllowed('Cannot move folder data: move the folder instead')
    
    def manage_DAVget(self):
        """DAV content access: delete to manage_FTPget()
        """
        return self.__parent__.manage_DAVget()
    
    def manage_FTPget(self):
        """FTP access: delegate to parent
        """
        return self.__parent__.manage_FTPget()
    
    def listDAVObjects(self):
        """DAV object listing: return nothing
        """
        return []


class StringStreamIterator(object):
    """Simple stream iterator to allow efficient data streaming.
    """
    
    # Stupid workaround for the fact that on Zope < 2.12, we don't have
    # a real interface
    if IInterface.providedBy(IStreamIterator):
        implements(IStreamIterator)
    else:
        __implements__ = (IStreamIterator,)
    
    def __init__(self, data, size=None, chunk=1<<16):
        """Consume data (a str) into a temporary file and prepare streaming.
        
        size is the length of the data. If not given, the length of the data
        string is used.
        
        chunk is the chunk size for the iterator
        """
        f = tempfile.TemporaryFile(mode='w+b')
        f.write(data)
        
        if size is not None:
            assert size == f.tell(), 'Size argument does not match data length'
        else:
            size = f.tell()
        
        f.seek(0)
        
        self.file = f
        self.size = size
        self.chunk = chunk
    
    def __iter__(self):
        return self
    
    def next(self):
        data = self.file.read(self.chunk)
        if not data:
            self.file.close()
            raise StopIteration
        return data
    
    def __len__(self):
        return self.size


class DefaultDirectoryFactory(object):
    """Default directory factory, invoked when an FTP/WebDAV operation
    attempts to create a new folder via a MKCOL request.
    
    The default implementation simply calls manage_addFolder().
    """
    
    implements(IDirectoryFactory)
    adapts(IDexterityContainer)
    
    def __init__(self, context):
        self.context = context
    
    def __call__(self, name):
        self.context.manage_addFolder(name)


class DefaultFileFactory(object):
    """Default file factory, invoked when an FTP/WebDAV operation
    attempts to create a new resource via a PUT request.
    
    The default implementation uses the content_type_registry to find a
    type to add, and then creates an instance using the portal_types
    tool.
    """
    
    implements(IFileFactory)
    adapts(IDexterityContainer)
    
    def __init__(self, context):
        self.context = context
    
    def __call__(self, name, contentType, data):
        
        # Deal with Finder cruft
        if name == '.DS_Store':
            raise Unauthorized("Refusing to store Mac OS X resource forks")
        elif name.startswith('._'):
            raise Unauthorized("Refusing to store Mac OS X resource forks")
        
        registry = getToolByName(self.context, 'content_type_registry', None)
        if registry is None:
            return None # fall back on default
        
        typeObjectName = registry.findTypeName(name, contentType, data)
        if typeObjectName is None:
            return # fall back on default
        
        typesTool = getToolByName(self.context, 'portal_types')
        
        targetType = typesTool.getTypeInfo(typeObjectName)
        if targetType is None:
            return # fall back on default
        
        contextType = typesTool.getTypeInfo(self.context)
        if contextType is not None:
            if not contextType.allowType(typeObjectName):
                raise Unauthorized("Creating a %s object here is not allowed" % typeObjectName)
        
        if not targetType.isConstructionAllowed(self.context):
            raise Unauthorized("Creating a %s object here is not allowed" % typeObjectName)
        
        # There are two possibilities here: either we have a new-style
        # IFactory utility, in which case all is good. We can call the
        # factory and return the object. Or, we have an old style factory
        # method which will call _setObject() magically. This results in all
        # sorts of events being fired, and then we have to delete the object,
        # before re-creating it immediately afterwards in NullResource.PUT().
        # Naturally this sucks. At least, let's do the sane thing for content
        # with new-style factories.
        
        if targetType.product: # boo :(
            m = targetType._getFactoryMethod(self.context, check_security=0)
            if getattr(aq_base(m), 'isDocTemp', 0):
                newid = m(m.aq_parent, self.context.REQUEST, id=name)
            else:
                newid = m(name)
            # allow factory to munge ID
            newid = newid or name
            
            newObj = self.context._getOb(newid)
            targetType._finishConstruction(newObj)
            
            # sob, sob...
            obj = aq_base(newObj)
            self.context._delObject(newid)
            
        else: # yay
            factory = getUtility(IFactory, targetType.factory)
            obj = factory(name)
            
            # we fire this event here, because NullResource.PUT will now go
            # and set the object on the parent. The correct sequence of
            # events is object created -> object added. In this case, we'll
            # get object created -> object added -> object modified.
            notify(ObjectCreatedEvent(obj))
        
        # mark object so that we can call _finishConstruction later
        
        return obj


class DefaultReadFile(object):
    """IRawReadFile adapter for Dexterity objects.
    
    Uses RFC822 marshaler.
    
    This is also marked as an IStreamIterator, which means that it is safe
    to return it to the publisher directly. In particular, the size() method
    will return an accurate file size.
    """
    
    # Stupid workaround for the fact that on Zope < 2.12, we don't have
    # a real interface
    if IInterface.providedBy(IStreamIterator):
        implements(IRawReadFile, IStreamIterator)
    else:
        implements(IRawReadFile)
        __implements__ = (IStreamIterator,)        
    
    adapts(IDexterityContent)
    
    def __init__(self, context):
        self.context = context
        self._haveMessage = False
    
    @property
    def mimeType(self):
        if not self._haveMessage:
            foundOne = False
            for schema in iterSchemata(self.context):
                for name, field in getFieldsInOrder(schema):
                    if IPrimaryField.providedBy(field):
                        if foundOne:
                            # more than one primary field
                            return 'message/rfc822'
                        else:
                            foundOne = True
            # zero or one primary fields
            return 'text/plain'
        if not self._getMessage().is_multipart():
            return 'text/plain'
        else:
            return 'message/rfc822'
    
    @property
    def encoding(self):
        return self._getMessage().get_charset() or 'utf-8'
    
    @property
    def closed(self):
        return self._getStream().closed
    
    @property
    def name(self):
        return self._getMessage().get_filename(None)
    
    def size(self):
        # construct the stream if necessary
        self._getStream()
        return self._size
    
    def seek(self, offset, whence=None):
        if whence is not None:
            self._getStream().seek(offset, whence)
        else:
            self._getStream().seek(offset)
    
    def tell(self):
        return self._getStream().tell()
    
    def close(self):
        self._getStream().close()
    
    def read(self, size=None):
        if size is not None:
            return self._getStream().read(size)
        else:
            return self._getStream().read()
    
    def readline(self, size=None):
        if size is None:
            return self._getStream().readline()
        else:
            return self._getStream().readline(size)
    
    def readlines(self, sizehint=None):
        if sizehint is None:
            return self._getStream().readlines()
        else:
            return self._getStream().readlines(sizehint)
    
    def __iter__(self):
        return self
    
    def next(self):
        return self._getStream().next()
    
    # internal helper methods
    
    @memoize
    def _getMessage(self):
        """Construct message on demand
        """
        message = constructMessageFromSchemata(self.context, iterSchemata(self.context))
        
        # Store the portal type in a header, to allow it to be identifed later
        message['Portal-Type'] = self.context.portal_type
        
        return message
    
    @memoize
    def _getStream(self):
        # We write to a TemporayFile instead of a StringIO because we don't
        # want to keep the full file contents around in memory, and because
        # this approach allows us to hand off the stream iterator to the
        # publisher, which will serve it efficiently even after the
        # transaction is closed
        out = tempfile.TemporaryFile(mode='w+b')
        generator = Generator(out, mangle_from_=False)
        generator.flatten(self._getMessage())
        self._size = out.tell()
        out.seek(0)
        return out


class DefaultWriteFile(object):
    """IRawWriteFile file adapter for Dexterity objects.
    
    Uses RFC822 marshaler.
    """
    
    implements(IRawWriteFile)
    adapts(IDexterityContent)
    
    def __init__(self, context):
        self.context = context
        
        self._mimeType = None
        self._encoding = 'utf-8'
        self._closed = False
        self._name = None
        self._written = 0
        self._parser = FeedParser()
        self._message = None
    
    @getproperty
    def mimeType(self):
        if self._message is None:
            return self._mimeType
        elif not self._message.is_multipart():
            return 'text/plain'
        else:
            return 'message/rfc822'
    
    @setproperty
    def mimeType(self, value):
        self._mimeType = value
    
    @getproperty
    def encoding(self):
        if self._message is not None:
            return self._message.get_charset() or self._encoding
        return self._encoding
    
    @setproperty
    def encoding(self, value):
        self._encoding = value
    
    @property
    def closed(self):
        return self._closed
    
    @getproperty
    def name(self):
        if self._message is not None:
            return self._message.get_filename(self._name)
        return self._name
    @setproperty
    def name(self, value):
        self._name = value
    
    def seek(self, offset, whence=None):
        raise NotImplementedError("Seeking is not supported")
    
    def tell(self):
        return self._written
    
    def close(self):
        self._message = self._parser.close()
        self._closed = True
        initializeObjectFromSchemata(self.context, iterSchemata(self.context), self._message, self._encoding)
    
    def write(self, data):
        if self._closed:
            raise ValueError("File is closed")
        self._written += len(data)
        self._parser.feed(data)
    
    def writelines(self, sequence):
        for item in sequence:
            self.write(item)
    
    def truncate(self, size=None):
        if (size is None and self._written != 0) and size != 0:
            raise NotImplementedError("The 'size' argument to truncate() must be 0 - partial truncation is not supported")
        if self._closed:
            raise ValueError("File is closed")
        self._parser = FeedParser()
        self._written = 0
    
    def flush(self):
        pass

