# coding: utf-8

from urllib.parse import urljoin

from geoserver.support import ResourceInfo, xml_property


class Style(ResourceInfo):
    supported_formats = ['sld10', 'sld11', 'zip']
    content_types = {
        "sld10": "application/vnd.ogc.sld+xml",
        "sld11": "application/vnd.ogc.se+xml",
        "zip": "application/zip"
    }

    def __init__(self, catalog, name, workspace=None, style_format='sld10'):
        super(Style, self).__init__()
        assert isinstance(name, str)
        assert style_format in self.supported_formats
        self.catalog = catalog
        self.workspace = workspace
        self.name = name
        self.style_format = style_format
        self._sld_dom = None

    @property
    def fqn(self):
        if not self.workspace:
            return self.name
        return '{}:{}'.format(self.workspace, self.name)

    @property
    def href(self):
        return self._build_href('.xml')

    @property
    def body_href(self):
        return self._build_href('.sld')

    @property
    def create_href(self):
        return self._build_href('.xml', True)

    @property
    def content_type(self):
        return Style.content_types[self.style_format]

    def _build_href(self, extension, create=False):
        url_part = "styles/"
        if not create:
            url_part += "{}{}".format(self.name, extension)
        else:
            url_part += "?name={}".format(self.name)
        if self.workspace is not None:
            url_part = "workspaces/{}/{}".format(
                getattr(self.workspace, 'name', self.workspace),
                url_part
            )
        return urljoin(
            self.catalog.service_url,
            url_part
        )

    filename = xml_property("filename")

    def _get_sld_dom(self):
        if self._sld_dom is None:
            self._sld_dom = self.catalog.get_xml(self.body_href)
        return self._sld_dom

    @property
    def sld_title(self):
        user_style = self._get_sld_dom().find("{http://www.opengis.net/sld}NamedLayer"
                                              "/{http://www.opengis.net/sld}UserStyle")
        if not user_style:
            user_style = self._get_sld_dom().find("{http://www.opengis.net/sld}UserLayer"
                                                  "/{http://www.opengis.net/sld}UserStyle")
        if user_style:
            try:
                # it is not mandatory
                title_node = user_style.find("{http://www.opengis.net/sld}Title")
            except Exception:
                title_node = None
        if title_node is not None:
            title_node = title_node.text
        return title_node

    @property
    def sld_name(self):
        user_style = self._get_sld_dom().find("{http://www.opengis.net/sld}NamedLayer/"
                                              "{http://www.opengis.net/sld}UserStyle")
        if not user_style:
            user_style = self._get_sld_dom().find("{http://www.opengis.net/sld}UserLayer/"
                                                  "{http://www.opengis.net/sld}UserStyle")
        if user_style:
            try:
                # it is not mandatory
                name_node = user_style.find("{http://www.opengis.net/sld}Name")
            except Exception:
                name_node = None
        if name_node is not None:
            name_node = name_node.text
        return name_node

    @property
    def sld_body(self):
        return self.catalog.session.get(self.body_href).text

    def update_body(self, body):
        headers = {"Content-Type": self.content_type}
        self.catalog.session.put(self.body_href, data=body, headers=headers)
