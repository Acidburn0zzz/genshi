# -*- coding: utf-8 -*-
#
# Copyright (C) 2006 Christopher Lenz
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at http://trac.edgewall.com/license.html.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at http://projects.edgewall.com/trac/.

"""Template engine that is compatible with Kid (http://kid.lesscode.org) to a
certain extent.

Differences include:
 * No generation of Python code for a template; the template is "interpreted"
 * No support for <?python ?> processing instructions
 * Expressions are evaluated in a more flexible manner, meaning you can use e.g.
   attribute access notation to access items in a dictionary, etc
 * Use of XInclude and match templates instead of Kid's py:extends/py:layout
   directives
 * Real (thread-safe) search path support
 * No dependency on ElementTree (due to the lack of pos info)
 * The original pos of parse events is kept throughout the processing
   pipeline, so that errors can be tracked back to a specific line/column in
   the template file
 * py:match directives use (basic) XPath expressions to match against input
   nodes, making match templates more powerful while keeping the syntax simple

Todo items:
 * Improved error reporting
 * Support for using directives as elements and not just as attributes, reducing
   the need for wrapper elements with py:strip=""
 * Support for py:choose/py:when/py:otherwise (similar to XSLT)
 * Support for list comprehensions and generator expressions in expressions

Random thoughts:
 * Is there any need to support py:extends and/or py:layout?
 * Could we generate byte code from expressions?
"""

import compiler
from itertools import chain
import os
import re
from StringIO import StringIO

from markup.core import Attributes, Stream, StreamEventKind
from markup.eval import Expression
from markup.filters import EvalFilter, IncludeFilter, MatchFilter, \
                           WhitespaceFilter
from markup.input import HTML, XMLParser, XML

__all__ = ['Context', 'BadDirectiveError', 'TemplateError',
           'TemplateSyntaxError', 'TemplateNotFound', 'Template',
           'TemplateLoader']


class TemplateError(Exception):
    """Base exception class for errors related to template processing."""


class TemplateSyntaxError(TemplateError):
    """Exception raised when an expression in a template causes a Python syntax
    error."""

    def __init__(self, message, filename='<string>', lineno=-1, offset=-1):
        if isinstance(message, SyntaxError) and message.lineno is not None:
            message = str(message).replace(' (line %d)' % message.lineno, '')
        TemplateError.__init__(self, message)
        self.filename = filename
        self.lineno = lineno
        self.offset = offset


class BadDirectiveError(TemplateSyntaxError):
    """Exception raised when an unknown directive is encountered when parsing
    a template.
    
    An unknown directive is any attribute using the namespace for directives,
    with a local name that doesn't match any registered directive.
    """

    def __init__(self, name, filename='<string>', lineno=-1):
        TemplateSyntaxError.__init__(self, 'Bad directive "%s"' % name.localname,
                                     filename, lineno)


class TemplateNotFound(TemplateError):
    """Exception raised when a specific template file could not be found."""

    def __init__(self, name, search_path):
        TemplateError.__init__(self, 'Template "%s" not found' % name)
        self.search_path = search_path


class Context(object):
    """A container for template input data.
    
    A context provides a stack of scopes. Template directives such as loops can
    push a new scope on the stack with data that should only be available
    inside the loop. When the loop terminates, that scope can get popped off
    the stack again.
    
    >>> ctxt = Context(one='foo', other=1)
    >>> ctxt.get('one')
    'foo'
    >>> ctxt.get('other')
    1
    >>> ctxt.push(one='frost')
    >>> ctxt.get('one')
    'frost'
    >>> ctxt.get('other')
    1
    >>> ctxt.pop()
    >>> ctxt.get('one')
    'foo'
    """

    def __init__(self, **data):
        self.stack = [data]

    def __getitem__(self, key):
        """Get a variable's value, starting at the current context and going
        upward.
        """
        return self.get(key)

    def __repr__(self):
        return repr(self.stack)

    def __setitem__(self, key, value):
        """Set a variable in the current context."""
        self.stack[0][key] = value

    def get(self, key):
        for frame in self.stack:
            if key in frame:
                return frame[key]

    def push(self, **data):
        self.stack.insert(0, data)

    def pop(self):
        assert self.stack, 'Pop from empty context stack'
        self.stack.pop(0)


class Directive(object):
    """Abstract base class for template directives.
    
    A directive is basically a callable that takes two parameters: `ctxt` is
    the template data context, and `stream` is an iterable over the events that
    the directive applies to.
    
    Directives can be "anonymous" or "registered". Registered directives can be
    applied by the template author using an XML attribute with the
    corresponding name in the template. Such directives should be subclasses of
    this base class that can  be instantiated with two parameters: `template`
    is the `Template` instance, and `value` is the value of the directive
    attribute.
    
    Anonymous directives are simply functions conforming to the protocol
    described above, and can only be applied programmatically (for example by
    template filters).
    """
    __slots__ = ['expr']

    def __init__(self, template, value, pos):
        self.expr = value and Expression(value) or None

    def __call__(self, stream, ctxt):
        raise NotImplementedError

    def __repr__(self):
        expr = ''
        if self.expr is not None:
            expr = ' "%s"' % self.expr.source
        return '<%s%s>' % (self.__class__.__name__, expr)


class AttrsDirective(Directive):
    """Implementation of the `py:attrs` template directive.
    
    The value of the `py:attrs` attribute should be a dictionary. The keys and
    values of that dictionary will be added as attributes to the element:
    
    >>> ctxt = Context(foo={'class': 'collapse'})
    >>> tmpl = Template('''<ul xmlns:py="http://purl.org/kid/ns#">
    ...   <li py:attrs="foo">Bar</li>
    ... </ul>''')
    >>> print tmpl.generate(ctxt)
    <ul>
      <li class="collapse">Bar</li>
    </ul>
    
    If the value evaluates to `None` (or any other non-truth value), no
    attributes are added:
    
    >>> ctxt = Context(foo=None)
    >>> print tmpl.generate(ctxt)
    <ul>
      <li>Bar</li>
    </ul>
    """
    def __call__(self, stream, ctxt):
        kind, (tag, attrib), pos  = stream.next()
        attrs = self.expr.evaluate(ctxt)
        if attrs:
            attrib = Attributes(attrib[:])
            if not isinstance(attrs, list): # assume it's a dict
                attrs = attrs.items()
            for name, value in attrs:
                if value is None:
                    attrib.remove(name)
                else:
                    attrib.set(name, unicode(value).strip())
        yield kind, (tag, attrib), pos
        for event in stream:
            yield event


class ContentDirective(Directive):
    """Implementation of the `py:content` template directive.
    
    This directive replaces the content of the element with the result of
    evaluating the value of the `py:content` attribute:
    
    >>> ctxt = Context(bar='Bye')
    >>> tmpl = Template('''<ul xmlns:py="http://purl.org/kid/ns#">
    ...   <li py:content="bar">Hello</li>
    ... </ul>''')
    >>> print tmpl.generate(ctxt)
    <ul>
      <li>Bye</li>
    </ul>
    """
    def __call__(self, stream, ctxt):
        kind, data, pos = stream.next()
        if kind is Stream.START:
            yield kind, data, pos # emit start tag
        yield Template.EXPR, self.expr, pos
        previous = stream.next()
        for event in stream:
            previous = event
        if previous is not None:
            yield previous


class DefDirective(Directive):
    """Implementation of the `py:def` template directive.
    
    This directive can be used to create "Named Template Functions", which
    are template snippets that are not actually output during normal
    processing, but rather can be expanded from expressions in other places
    in the template.
    
    A named template function can be used just like a normal Python function
    from template expressions:
    
    >>> ctxt = Context(bar='Bye')
    >>> tmpl = Template('''<div xmlns:py="http://purl.org/kid/ns#">
    ...   <p py:def="echo(greeting, name='world')" class="message">
    ...     ${greeting}, ${name}!
    ...   </p>
    ...   ${echo('hi', name='you')}
    ... </div>''')
    >>> print tmpl.generate(ctxt)
    <div>
      <p class="message">
        hi, you!
      </p>
    </div>
    
    >>> ctxt = Context(bar='Bye')
    >>> tmpl = Template('''<div xmlns:py="http://purl.org/kid/ns#">
    ...   <p py:def="echo(greeting, name='world')" class="message">
    ...     ${greeting}, ${name}!
    ...   </p>
    ...   <div py:replace="echo('hello')"></div>
    ... </div>''')
    >>> print tmpl.generate(ctxt)
    <div>
      <p class="message">
        hello, world!
      </p>
    </div>
    """
    __slots__ = ['name', 'args', 'defaults', 'stream']

    def __init__(self, template, args, pos):
        Directive.__init__(self, template, None, pos)
        ast = compiler.parse(args, 'eval').node
        self.args = []
        self.defaults = {}
        if isinstance(ast, compiler.ast.CallFunc):
            self.name = ast.node.name
            for arg in ast.args:
                if isinstance(arg, compiler.ast.Keyword):
                    self.args.append(arg.name)
                    self.defaults[arg.name] = arg.expr.value
                else:
                    self.args.append(arg.name)
        else:
            self.name = ast.name
        self.stream = []

    def __call__(self, stream, ctxt):
        self.stream = list(stream)
        ctxt[self.name] = lambda *args, **kwargs: self._exec(ctxt, *args,
                                                             **kwargs)
        return []

    def _exec(self, ctxt, *args, **kwargs):
        scope = {}
        args = list(args) # make mutable
        for name in self.args:
            if args:
                scope[name] = args.pop(0)
            else:
                scope[name] = kwargs.pop(name, self.defaults.get(name))
        ctxt.push(**scope)
        for event in self.stream:
            yield event
        ctxt.pop()


class ForDirective(Directive):
    """Implementation of the `py:for` template directive.
    
    >>> ctxt = Context(items=[1, 2, 3])
    >>> tmpl = Template('''<ul xmlns:py="http://purl.org/kid/ns#">
    ...   <li py:for="item in items">${item}</li>
    ... </ul>''')
    >>> print tmpl.generate(ctxt)
    <ul>
      <li>1</li><li>2</li><li>3</li>
    </ul>
    """
    __slots__ = ['targets']

    def __init__(self, template, value, pos):
        targets, expr_source = value.split(' in ', 1)
        self.targets = [str(name.strip()) for name in targets.split(',')]
        Directive.__init__(self, template, expr_source, pos)

    def __call__(self, stream, ctxt):
        iterable = self.expr.evaluate(ctxt, [])
        if iterable is not None:
            stream = list(stream)
            for item in iter(iterable):
                if len(self.targets) == 1:
                    item = [item]
                scope = {}
                for idx, name in enumerate(self.targets):
                    scope[name] = item[idx]
                ctxt.push(**scope)
                for event in stream:
                    yield event
                ctxt.pop()

    def __repr__(self):
        return '<%s "%s in %s">' % (self.__class__.__name__,
                                    ', '.join(self.targets), self.expr.source)


class IfDirective(Directive):
    """Implementation of the `py:if` template directive.
    
    >>> ctxt = Context(foo=True, bar='Hello')
    >>> tmpl = Template('''<div xmlns:py="http://purl.org/kid/ns#">
    ...   <b py:if="foo">${bar}</b>
    ... </div>''')
    >>> print tmpl.generate(ctxt)
    <div>
      <b>Hello</b>
    </div>
    """
    def __call__(self, stream, ctxt):
        if self.expr.evaluate(ctxt):
            return stream
        return []


class MatchDirective(Directive):
    """Implementation of the `py:match` template directive.
    
    >>> ctxt = Context()
    >>> tmpl = Template('''<div xmlns:py="http://purl.org/kid/ns#">
    ...   <span py:match="div/greeting">
    ...     Hello ${select('@name')}
    ...   </span>
    ...   <greeting name="Dude" />
    ... </div>''')
    >>> print tmpl.generate(ctxt)
    <div>
      <span>
        Hello Dude
      </span>
    </div>
    """
    __slots__ = ['path', 'stream']

    def __init__(self, template, value, pos):
        Directive.__init__(self, template, None, pos)
        template.filters.append(MatchFilter(value, self._handle_match))
        self.path = value
        self.stream = []

    def __call__(self, stream, ctxt):
        self.stream = list(stream)
        return []

    def __repr__(self):
        return '<%s "%s">' % (self.__class__.__name__, self.path)

    def _handle_match(self, orig_stream, ctxt):
        ctxt.push(select=lambda path: Stream(orig_stream).select(path))
        for event in self.stream:
            yield event
        ctxt.pop()


class ReplaceDirective(Directive):
    """Implementation of the `py:replace` template directive.
    
    >>> ctxt = Context(bar='Bye')
    >>> tmpl = Template('''<div xmlns:py="http://purl.org/kid/ns#">
    ...   <span py:replace="bar">Hello</span>
    ... </div>''')
    >>> print tmpl.generate(ctxt)
    <div>
      Bye
    </div>
    
    This directive is equivalent to `py:content` combined with `py:strip`,
    providing a less verbose way to achieve the same effect:
    
    >>> ctxt = Context(bar='Bye')
    >>> tmpl = Template('''<div xmlns:py="http://purl.org/kid/ns#">
    ...   <span py:content="bar" py:strip="">Hello</span>
    ... </div>''')
    >>> print tmpl.generate(ctxt)
    <div>
      Bye
    </div>
    """
    def __call__(self, stream, ctxt):
        kind, data, pos = stream.next()
        yield Template.EXPR, self.expr, pos


class StripDirective(Directive):
    """Implementation of the `py:strip` template directive.
    
    When the value of the `py:strip` attribute evaluates to `True`, the element
    is stripped from the output
    
    >>> ctxt = Context()
    >>> tmpl = Template('''<div xmlns:py="http://purl.org/kid/ns#">
    ...   <div py:strip="True"><b>foo</b></div>
    ... </div>''')
    >>> print tmpl.generate(ctxt)
    <div>
      <b>foo</b>
    </div>
    
    On the other hand, when the attribute evaluates to `False`, the element is
    not stripped:
    
    >>> ctxt = Context()
    >>> tmpl = Template('''<div xmlns:py="http://purl.org/kid/ns#">
    ...   <div py:strip="False"><b>foo</b></div>
    ... </div>''')
    >>> print tmpl.generate(ctxt)
    <div>
      <div><b>foo</b></div>
    </div>
    
    Leaving the attribute value empty is equivalent to a truth value:
    
    >>> ctxt = Context()
    >>> tmpl = Template('''<div xmlns:py="http://purl.org/kid/ns#">
    ...   <div py:strip=""><b>foo</b></div>
    ... </div>''')
    >>> print tmpl.generate(ctxt)
    <div>
      <b>foo</b>
    </div>
    
    This directive is particulary interesting for named template functions or
    match templates that do not generate a top-level element:
    
    >>> ctxt = Context()
    >>> tmpl = Template('''<div xmlns:py="http://purl.org/kid/ns#">
    ...   <div py:def="echo(what)" py:strip="">
    ...     <b>${what}</b>
    ...   </div>
    ...   ${echo('foo')}
    ... </div>''')
    >>> print tmpl.generate(ctxt)
    <div>
        <b>foo</b>
    </div>
    """
    def __call__(self, stream, ctxt):
        if self.expr:
            strip = self.expr.evaluate(ctxt)
        else:
            strip = True
        if strip:
            stream.next() # skip start tag
            previous = stream.next()
            for event in stream:
                yield previous
                previous = event
        else:
            for event in stream:
                yield event


class Template(object):
    """Can parse a template and transform it into the corresponding output
    based on context data.
    """
    NAMESPACE = 'http://purl.org/kid/ns#'

    EXPR = StreamEventKind('expr') # an expression
    SUB = StreamEventKind('sub') # a "subprogram"

    directives = [('def', DefDirective),
                  ('match', MatchDirective),
                  ('for', ForDirective),
                  ('if', IfDirective),
                  ('replace', ReplaceDirective),
                  ('content', ContentDirective),
                  ('attrs', AttrsDirective),
                  ('strip', StripDirective)]
    _dir_by_name = dict(directives)
    _dir_order = [directive[1] for directive in directives]

    def __init__(self, source, filename=None):
        """Initialize a template from either a string or a file-like object."""
        if isinstance(source, basestring):
            self.source = StringIO(source)
        else:
            self.source = source
        self.filename = filename or '<string>'

        self.pre_filters = [EvalFilter()]
        self.filters = []
        self.post_filters = [WhitespaceFilter()]
        self.parse()

    def __repr__(self):
        return '<%s "%s">' % (self.__class__.__name__,
                              os.path.basename(self.filename))

    def parse(self):
        """Parse the template.
        
        The parsing stage parses the XML template and constructs a list of
        directives that will be executed in the render stage. The input is
        split up into literal output (markup that does not depend on the
        context data) and actual directives (commands or variable
        substitution).
        """
        stream = [] # list of events of the "compiled" template
        dirmap = {} # temporary mapping of directives to elements
        ns_prefix = {}
        depth = 0

        for kind, data, pos in XMLParser(self.source):

            if kind is Stream.START_NS:
                # Strip out the namespace declaration for template directives
                prefix, uri = data
                if uri == self.NAMESPACE:
                    ns_prefix[prefix] = uri
                else:
                    stream.append((kind, data, pos))

            elif kind is Stream.END_NS:
                if data in ns_prefix:
                    del ns_prefix[data]
                else:
                    stream.append((kind, data, pos))

            elif kind is Stream.START:
                # Record any directive attributes in start tags
                tag, attrib = data
                directives = []
                new_attrib = []
                for name, value in attrib:
                    if name.namespace == self.NAMESPACE:
                        cls = self._dir_by_name.get(name.localname)
                        if cls is None:
                            raise BadDirectiveError(name, self.filename, pos[0])
                        else:
                            directives.append(cls(self, value, pos))
                    else:
                        value = list(self._interpolate(value, *pos))
                        new_attrib.append((name, value))
                if directives:
                    directives.sort(lambda a, b: cmp(self._dir_order.index(a.__class__),
                                                     self._dir_order.index(b.__class__)))
                    dirmap[(depth, tag)] = (directives, len(stream))

                stream.append((kind, (tag, Attributes(new_attrib)), pos))
                depth += 1

            elif kind is Stream.END:
                depth -= 1
                stream.append((kind, data, pos))

                # If there have have directive attributes with the corresponding
                # start tag, move the events inbetween into a "subprogram"
                if (depth, data) in dirmap:
                    directives, start_offset = dirmap.pop((depth, data))
                    substream = stream[start_offset:]
                    stream[start_offset:] = [(Template.SUB,
                                              (directives, substream), pos)]

            elif kind is Stream.TEXT:
                for kind, data, pos in self._interpolate(data, *pos):
                    stream.append((kind, data, pos))

            else:
                stream.append((kind, data, pos))

        self.stream = stream

    def generate(self, ctxt):
        """Transform the template based on the given context data."""

        def _transform(stream):
            # Apply pre and runtime filters
            for filter_ in chain(self.pre_filters, self.filters):
                stream = filter_(iter(stream), ctxt)

            try:
                for kind, data, pos in stream:

                    if kind is Template.SUB:
                        # This event is a list of directives and a list of
                        # nested events to which those directives should be
                        # applied
                        directives, substream = data
                        directives.reverse()
                        for directive in directives:
                            substream = directive(iter(substream), ctxt)
                        for event in _transform(iter(substream)):
                            yield event

                    else:
                        yield kind, data, pos
            except SyntaxError, err:
                raise TemplateSyntaxError(err, self.filename, pos[0],
                                          pos[1] + (err.offset or 0))

        stream = _transform(self.stream)

        # Apply post-filters
        for filter_ in self.post_filters:
            stream = filter_(iter(stream), ctxt)

        return Stream(stream)

    _FULL_EXPR_RE = re.compile(r'(?<!\$)\$\{(.+?)\}')
    _SHORT_EXPR_RE = re.compile(r'(?<!\$)\$([a-zA-Z][a-zA-Z0-9_\.]*)')

    def _interpolate(cls, text, lineno=-1, offset=-1):
        """Parse the given string and extract expressions.
        
        This method returns a list containing both literal text and `Expression`
        objects.

        @param text: the text to parse
        @param lineno: the line number at which the text was found (optional)
        @param offset: the column number at which the text starts in the source
            (optional)
        """
        patterns = [cls._FULL_EXPR_RE, cls._SHORT_EXPR_RE]
        def _interpolate(text):
            for idx, group in enumerate(patterns.pop(0).split(text)):
                if idx % 2:
                    yield Template.EXPR, Expression(group), (lineno, offset)
                elif group:
                    if patterns:
                        for result in _interpolate(group):
                            yield result
                    else:
                        yield Stream.TEXT, group.replace('$$', '$'), \
                              (lineno, offset)
        return _interpolate(text)
    _interpolate = classmethod(_interpolate)


class TemplateLoader(object):
    """Responsible for loading templates from files on the specified search
    path.
    
    >>> import tempfile
    >>> fd, path = tempfile.mkstemp(suffix='.html', prefix='template')
    >>> os.write(fd, '<p>$var</p>')
    11
    >>> os.close(fd)
    
    The template loader accepts a list of directory paths that are then used
    when searching for template files, in the given order:
    
    >>> loader = TemplateLoader([os.path.dirname(path)])
    
    The `load()` method first checks the template cache whether the requested
    template has already been loaded. If not, it attempts to locate the
    template file, and returns the corresponding `Template` object:
    
    >>> template = loader.load(os.path.basename(path))
    >>> isinstance(template, Template)
    True
    
    Template instances are cached: requesting a template with the same name
    results in the same instance being returned:
    
    >>> loader.load(os.path.basename(path)) is template
    True
    """
    def __init__(self, search_path=None, auto_reload=False):
        """Create the template laoder.
        
        @param search_path: a list of absolute path names that should be
            searched for template files
        @param auto_reload: whether to check the last modification time of
            template files, and reload them if they have changed
        """
        self.search_path = search_path
        if self.search_path is None:
            self.search_path = []
        self.auto_reload = auto_reload
        self._cache = {}
        self._mtime = {}

    def load(self, filename):
        """Load the template with the given name.
        
        This method searches the search path trying to locate a template
        matching the given name. If no such template is found, a
        `TemplateNotFound` exception is raised. Otherwise, a `Template` object
        representing the requested template is returned.
        
        Template searches are cached to avoid having to parse the same template
        file more than once. Thus, subsequent calls of this method with the
        same template file name will return the same `Template` object.
        
        @param filename: the relative path of the template file to load
        """
        filename = os.path.normpath(filename)
        try:
            tmpl = self._cache[filename]
            if not self.auto_reload or \
                    os.path.getmtime(tmpl.filename) == self._mtime[filename]:
                return tmpl
        except KeyError:
            pass
        for dirname in self.search_path:
            filepath = os.path.join(dirname, filename)
            try:
                fileobj = file(filepath, 'rt')
                try:
                    tmpl = Template(fileobj, filename=filepath)
                    tmpl.pre_filters.append(IncludeFilter(self, tmpl))
                finally:
                    fileobj.close()
                self._cache[filename] = tmpl
                self._mtime[filename] = os.path.getmtime(filepath)
                return tmpl
            except IOError:
                continue
        raise TemplateNotFound(filename, self.search_path)
