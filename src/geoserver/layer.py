# coding: utf-8

from urllib.parse import urljoin

from geoserver.support import ResourceInfo, xml_property, write_bool
from geoserver.style import Style
from geoserver import settings


class _Attribution:
    def __init__(self, title, width, height, href, url, logo_type):
        self.title = title
        self.width = width
        self.height = height
        self.href = href
        self.url = url
        self.logo_type = logo_type


def _read_attribution(node):
    title = node.find("title")
    width = node.find("logoWidth")
    height = node.find("logoHeight")
    href = node.find("href")
    url = node.find("logoURL")
    logo_type = node.find("logoType")
    if title is not None:
        title = title.text
    if width is not None:
        width = width.text
    if height is not None:
        height = height.text
    if href is not None:
        href = href.text
    if url is not None:
        url = url.text
    if logo_type is not None:
        logo_type = logo_type.text
    return _Attribution(title, width, height, href, url, logo_type)


def _write_attribution(builder, attribution):
    builder.start("attribution", dict())
    if attribution.title is not None:
        builder.start("title", dict())
        builder.data(attribution.title)
        builder.end("title")
    if attribution.width is not None:
        builder.start("logoWidth", dict())
        builder.data(attribution.width)
        builder.end("logoWidth")
    if attribution.height is not None:
        builder.start("logoHeight", dict())
        builder.data(attribution.height)
        builder.end("logoHeight")
    if attribution.href is not None:
        builder.start("href", dict())
        builder.data(attribution.href)
        builder.end("href")
    if attribution.url is not None:
        builder.start("logoURL", dict())
        builder.data(attribution.url)
        builder.end("logoURL")
    if attribution.logo_type is not None:
        builder.start("logoType", dict())
        builder.data(attribution.logo_type)
        builder.end("logoType")
    builder.end("attribution")


def _write_style_element(builder, name):
    ws, name = name.split(':') if ':' in name else (None, name)
    builder.start("name", dict())
    builder.data(name)
    builder.end("name")
    if ws:
        builder.start("workspace", dict())
        builder.data(ws)
        builder.end("workspace")


def _write_default_style(builder, name):
    builder.start("defaultStyle", dict())
    if name is not None:
        _write_style_element(builder, name)
    builder.end("defaultStyle")


def _write_alternate_styles(builder, styles):
    builder.start("styles", dict())
    for s in styles:
        builder.start("style", dict())
        _write_style_element(builder, getattr(s, 'fqn', s))
        builder.end("style")
    builder.end("styles")


class Layer(ResourceInfo):
    def __init__(self, catalog, name):
        super(Layer, self).__init__()
        self.catalog = catalog
        self.name = name

    resource_type = "layer"
    save_method = settings.PUT

    @property
    def href(self):
        return urljoin(
            self.catalog.service_url,
            "layers/{}.xml".format(self.name)
        )

    @property
    def resource(self):
        if self.dom is None:
            self.fetch()
        name = self.dom.find("resource/name").text
        return self.catalog.get_resource(name)

    @property
    def queryable(self):
        if self.dom is None:
            self.fetch()
        queryableEl = self.dom.find("queryable")
        if queryableEl is None:
            return True
        else:
            return (queryableEl.text == "true")

    @queryable.setter
    def queryable(self, queryable):
        self.dirty["queryable"] = queryable

    @property
    def opaque(self):
        if self.dom is None:
            self.fetch()
        opaqueEl = self.dom.find("opaque")
        if opaqueEl is None:
            return True
        else:
            return (opaqueEl.text == "true")

    @opaque.setter
    def opaque(self, opaque):
        self.dirty["opaque"] = opaque

    @property
    def default_style(self):
        if 'default_style' in self.dirty:
            return self.dirty['default_style']
        if self.dom is None:
            self.fetch()
        element = None
        if self.dom:
            element = self.dom.find("defaultStyle")
        # aborted data uploads can result in no default style
        return self._resolve_style(element) if element is not None else None

    @default_style.setter
    def default_style(self, style):
        if isinstance(style, Style):
            style = style.fqn
        self.dirty["default_style"] = style

    @property
    def styles(self):
        if "alternate_styles" in self.dirty:
            return self.dirty["alternate_styles"]
        if self.dom is None:
            self.fetch()
        if self.dom:
            styles_list = self.dom.findall("styles/style")
        else:
            styles_list = []
        return filter(None, [self._resolve_style(s) for s in styles_list])

    @styles.setter
    def styles(self, styles):
        self.dirty["alternate_styles"] = styles

    def _resolve_style(self, element):
        # instead of using name or the workspace element (which only appears
        # in >=2.4), just use the atom link href attribute
        atom_link = [n for n in element.getchildren() if 'href' in n.attrib]
        if atom_link:
            style_workspace_url = atom_link[0].attrib.get("href")
            return self.catalog.get_style_by_url(style_workspace_url)

    @property
    def attribution(self):
        return self.attribution_object.title

    @attribution.setter
    def attribution(self, text):
        self.dirty["attribution"] = _Attribution(
            text,
            self.attribution_object.width,
            self.attribution_object.height
        )
        assert self.attribution_object.title == text

    attribution_object = xml_property("attribution", _read_attribution)
    enabled = xml_property("enabled", lambda x: x.text == "true")
    advertised = xml_property("advertised", lambda x: x.text == "true",
                              default=True)

    def _get_attr_attribution(self):
        return {'title': self.attribution_object.title,
                'width': self.attribution_object.width,
                'height': self.attribution_object.height,
                'href': self.attribution_object.href,
                'url': self.attribution_object.url,
                'type': self.attribution_object.logo_type}

    def _set_attr_attribution(self, attribution):
        self.dirty["attribution"] = _Attribution(attribution['title'],
                                                 attribution['width'],
                                                 attribution['height'],
                                                 attribution['href'],
                                                 attribution['url'],
                                                 attribution['type'])

        assert self.attribution_object.title == attribution['title']
        assert self.attribution_object.width == attribution['width']
        assert self.attribution_object.height == attribution['height']
        assert self.attribution_object.href == attribution['href']
        assert self.attribution_object.url == attribution['url']
        assert self.attribution_object.logo_type == attribution['type']

    attribution = property(_get_attr_attribution, _set_attr_attribution)

    writers = {
        'attribution': _write_attribution,
        'enabled': write_bool("enabled"),
        'advertised': write_bool("advertised"),
        'default_style': _write_default_style,
        'alternate_styles': _write_alternate_styles,
        'queryable': write_bool("queryable"),
        'opaque': write_bool("opaque")
    }
