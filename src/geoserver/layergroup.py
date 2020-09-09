# coding: utf-8

from urllib.parse import urljoin
from urllib import url

from geoserver.support import ResourceInfo, write_string, write_bbox, \
    xml_property, bbox
from geoserver import settings


def _maybe_text(n):
    if n is None:
        return None
    else:
        return n.text


def _layer_list(node, element):
    if node is not None:
        return [_maybe_text(n.find("name")) for n in node.findall(element)]


def _style_list(node):
    if node is not None:
        return [_maybe_text(n.find("name")) for n in node.findall("style")]


def _write_layers(builder, layers, parent, element, attributes):
    builder.start(parent, dict())
    for l in layers:
        builder.start(element, attributes or dict())
        if l is not None:
            builder.start("name", dict())
            builder.data(l)
            builder.end("name")
        builder.end(element)
    builder.end(parent)


def _write_styles(builder, styles):
    builder.start("styles", dict())
    for s in styles:
        builder.start("style", dict())
        if s is not None:
            builder.start("name", dict())
            builder.data(s)
            builder.end("name")
        builder.end("style")
    builder.end("styles")


class LayerGroup(ResourceInfo):
    """    Represents a layer group in geoserver
    """

    resource_type = "layerGroup"
    save_method = settings.PUT

    def __init__(self, catalog, name, workspace=None):
        super(LayerGroup, self).__init__()
        assert isinstance(name, str)
        self.catalog = catalog
        self.name = name
        self.workspace = workspace
        # the XML format changed in 2.3.x - the element listing all the layers
        # and the entries themselves have changed
        if self.catalog.gsversion() == "2.2.x":
            parent, element, attributes = "layers", "layer", None
        else:
            parent = "publishables"
            element = "published"
            attributes = {'type': 'layer'}
        self._layer_parent = parent
        self._layer_element = element
        self._layer_attributes = attributes
        self.writers = {
            'name': write_string("name"),
            'styles': _write_styles,
            'layers': lambda b, l: _write_layers(b, l, parent,
                                                 element, attributes),
            'bounds': write_bbox("bounds"),
            'workspace': write_string("workspace"),
            'abstractTxt': write_string("abstractTxt"),
            'title': write_string("title")
        }

    @property
    def href(self):
        path_parts = "layergroups/{}.xml".format(self.name)
        if self.workspace is not None:
            workspace_name = getattr(self.workspace, 'name', self.workspace)
            path_parts = "workspaces/{}/{}".format(workspace_name, path_parts)
        return urljoin(
            self.catalog.service_url,
            path_parts
        )
        return url(self.catalog.service_url, path_parts)

    styles = xml_property("styles", _style_list)
    bounds = xml_property("bounds", bbox)
    abstract = xml_property("abstractTxt")
    title = xml_property("title")

    @property
    def layers(self):
        if "layers" in self.dirty:
            return self.dirty["layers"]
        else:
            if self.dom is None:
                self.fetch()
            node = self.dom.find(self._layer_parent)
            if node is not None:
                return _layer_list(node, self._layer_element)
            return None

    @layers.setter
    def _layers_setter(self, value):
        self.dirty["layers"] = value

    @layers.deleter
    def _layers_delete(self):
        self.dirty["layers"] = None

    def __str__(self):
        return "<LayerGroup {}>".format(self.name)

    __repr__ = __str__


class UnsavedLayerGroup(LayerGroup):

    save_method = settings.POST

    def __init__(self, catalog, name, layers, styles, bounds, abstract=None, title=None, workspace=None):
        super(UnsavedLayerGroup, self).__init__(
            catalog,
            name,
            workspace=workspace
        )
        if bounds is None:
            bounds = ("-180", "180", "-90", "90", "EPSG:4326")
        self.dirty.update(
            name=name,
            layers=layers,
            styles=styles,
            bounds=bounds,
            workspace=workspace,
            abstractTxt=abstract,
            title=title)

    @property
    def href(self):
        path_parts = 'layergroups'
        if self.workspace is not None:
            workspace_name = getattr(self.workspace, 'name', self.workspace)
            path_parts = "workspaces/{}/{}".format(workspace_name, path_parts)
        return urljoin(
            self.catalog.service_url,
            "{}?name={}".format(path_parts, self.name)
        )
