# coding: utf-8

from urllib.parse import urljoin
import os
import json
import logging
from datetime import datetime, timedelta
from xml.etree.ElementTree import XML
from xml.parsers.expat import ExpatError

import requests

from geoserver import settings
from geoserver.resource import FeatureType, Coverage
from geoserver.support import prepare_upload_bundle, _decode_dict
from geoserver.workspace import workspace_from_index, Workspace
from geoserver.store import coveragestore_from_index, \
    wmsstore_from_index, datastore_from_index, UnsavedDataStore, \
    UnsavedCoverageStore, UnsavedWmsStore
from geoserver.layer import Layer
from geoserver.layergroup import LayerGroup, UnsavedLayerGroup
from geoserver.style import Style


LOGGER = logging.getLogger("gsconfig2.catalog")


class Catalog:
    """
    The GeoServer catalog represents all of the information in the GeoServer
    configuration. This includes:
    - Stores of geospatial data
    - Resources, or individual coherent datasets within stores
    - Styles for resources
    - Layers, which combine styles with resources to create a visible map layer
    - LayerGroups, which alias one or more layers for convenience
    - Workspaces, which provide logical grouping of Stores
    - Maps, which provide a set of OWS services with a subset of the server's
        Layers
    - Namespaces, which provide unique identifiers for resources
    """

    def __init__(self, service_url,
                 username=settings.DEFAULT_USERNAME,
                 password=settings.DEFAULT_PASSWORD,
                 disable_ssl_certificate_validation=False):
        self._service_url = service_url
        self._username = username
        self._password = password
        self._disable_ssl_validation = disable_ssl_certificate_validation
        self._cache = dict()
        self._version = None
        self._session = requests.Session()
        self._session.auth = (self.username, self.password)

    @property
    def service_url(self):
        return self._service_url

    @property
    def username(self):
        return self._username

    @property
    def password(self):
        return self._password

    @property
    def disable_ssl_validation(self):
        return self._disable_ssl_validation

    @property
    def session(self):
        return self._session

    @property
    def version(self):
        return self._version

    def about(self):
        """
        :return: About informations of the geoserver as a formatted html.
        """
        about_url = urljoin(self.service_url, "about/version.xml")
        r = self.session.get(about_url)
        if r.status_code == requests.codes.ok:
            return r.text
        raise FailedRequestError("Unable to determine version: {}"
                                 .format(r.text or r.status_code))

    def gsversion(self):
        """
        :return: The geoserver version.
        """
        if self.version:
            return self.version
        else:
            about_text = self.about()
            dom = XML(about_text)
            resources = dom.findall("resource")
            version = None
            for resource in resources:
                if resource.attrib["name"] == "GeoServer":
                    try:
                        version = resource.find("Version").text
                        break
                    except AttributeError:
                        pass
            if version is None:
                version = "<2.3.x"
            self._version = version
            return version

    # TODO: Test
    def delete(self, config_object, purge=None, recurse=False):
        """
        send a delete request.
        :param recurse: True if underlying objects must be deleted recursively.
        :param purge:
        """
        rest_url = config_object.href
        params = {
            'purge': purge,
            'recurse': recurse,
        }
        headers = {
            "Content-type": "application/xml",
            "Accept": "application/xml"
        }
        r = self.session.delete(rest_url, params=params, headers=headers)
        if r.status_code == requests.codes.ok:
            return r.text
        else:
            msg = "Tried to make a DELETE request to "\
                  + "{} but got a {} status code: \n{}"\
                  .format(rest_url, r.status_code, r.text)
            raise FailedRequestError(msg)

    def get_xml(self, rest_url):
        LOGGER.debug("GET {}".format(rest_url))
        cached_response = self._cache.get(rest_url)
        if cached_response is not None:
            last_cache = datetime.now() - cached_response[0]
            text = cached_response[1]
        cache_expire = timedelta(seconds=5)
        if cached_response is None or last_cache > cache_expire:
            r = self.session.get(rest_url)
            text = r.text
            if r.status_code == requests.codes.ok:
                self._cache[rest_url] = (datetime.now(), text)
            else:
                msg = "Tried to make a GET request to {}"\
                      + " but got a {} status code: \n{}"
                raise FailedRequestError(msg.format(
                    rest_url,
                    r.status_code,
                    text
                ))
        try:
            return XML(text)
        except (ExpatError, SyntaxError) as e:
            msg = "GeoServer gave non-XML response for [GET {}]: {}"\
                  .format(rest_url, text)
            raise Exception(msg, e)

    def reload(self):
        """
        Send a reload request to the GeoServer and clear the cache.
        :return: The response given by the GeoServer.
        """
        reload_url = urljoin(self.service_url, "reload")
        r = self.session.post(reload_url)
        self._cache.clear()
        return r

    def reset(self):
        """
        Send a reset request to the GeoServer and clear the cache.
        :return: The response given by the server.
        """
        reset_url = urljoin(self.service_url, "reset")
        r = self.session.post(reset_url)
        self._cache.clear()
        return r

    def save(self, obj, content_type="application/xml"):
        """
        saves an object to the REST service
        gets the object's REST location and the data from the object,
        then POSTS the request.
        :param obj: The object to save.
        :return: The response given by the server.
        """
        rest_url = obj.href
        message = obj.message()
        save_method = obj.save_method
        LOGGER.debug("{} {}".format(save_method, rest_url))
        methods = {
            settings.POST: self.session.post,
            settings.PUT: self.session.put
        }
        headers = {
            "Content-type": content_type,
            "Accept": content_type
        }
        r = methods[save_method](rest_url, data=message, headers=headers)
        self._cache.clear()
        if 400 <= r.status_code < 600:
            raise FailedRequestError(
                "Error code ({}) from GeoServer: {}"
                .format(r.status_code, r.text)
            )
        return r

    def get_store(self, name, workspace=None):
        # Make sure workspace is a workspace object and not a string.
        # If the workspace does not exist,
        # continue as if no workspace had been defined.
        if isinstance(workspace, str):
            workspace = self.get_workspace(workspace)
        # Create a list with potential workspaces to look into
        # if a workspace is defined, it will contain only that workspace
        # if no workspace is defined, the list will contain all workspaces.
        workspaces = []
        if workspace is None:
            workspaces.extend(self.get_workspaces())
        else:
            workspaces.append(workspace)
        # Iterate over all workspaces to find the stores or store
        found_stores = {}
        for ws in workspaces:
            # Get all the store objects from geoserver
            raw_stores = self.get_stores(workspace=ws)
            # And put it in a dictionary where the keys are
            # the name of the store,
            new_stores = dict(zip([s.name for s in raw_stores], raw_stores))
            # If the store is found, put it in a dict that
            #  also takes into account the worspace.
            if name in new_stores:
                found_stores[ws.name + ':' + name] = new_stores[name]
        # There are 3 cases:
        #    a) No stores are found.
        #    b) Only one store is found.
        #    c) More than one is found.
        if len(found_stores) == 0:
            raise FailedRequestError("No store found named: {}".format(name))
        elif len(found_stores) > 1:
            msg = "Multiple stores found named '{}': {}"
            raise AmbiguousRequestError(msg.format(name, found_stores.keys()))
        else:
            return list(found_stores.values())[0]

    def get_stores(self, workspace=None):
        if workspace is not None:
            if isinstance(workspace, str):
                workspace = self.get_workspace(workspace)
            ds_list = self.get_xml(workspace.datastore_url)
            cs_list = self.get_xml(workspace.coveragestore_url)
            wms_list = self.get_xml(workspace.wmsstore_url)
            datastores = [datastore_from_index(self, workspace, n)
                          for n in ds_list.findall("dataStore")]
            coveragestores = [coveragestore_from_index(self, workspace, n)
                              for n in cs_list.findall("coverageStore")]
            wmsstores = [wmsstore_from_index(self, workspace, n)
                         for n in wms_list.findall("wmsStore")]
            return datastores + coveragestores + wmsstores
        else:
            stores = []
            for ws in self.get_workspaces():
                a = self.get_stores(ws)
                stores.extend(a)
            return stores

    def create_datastore(self, name, workspace=None):
        if isinstance(workspace, str):
            workspace = self.get_workspace(workspace)
        elif workspace is None:
            workspace = self.get_default_workspace()
        return UnsavedDataStore(self, name, workspace)

    def create_coveragestore2(self, name, workspace=None):
        """
        Hm we already named the method that creates a coverage *resource*
        create_coveragestore... time for an API break?
        """
        if isinstance(workspace, str):
            workspace = self.get_workspace(workspace)
        elif workspace is None:
            workspace = self.get_default_workspace()
        return UnsavedCoverageStore(self, name, workspace)

    def create_wmsstore(self, name, workspace=None):
        if workspace is None:
            workspace = self.get_default_workspace()
        return UnsavedWmsStore(self, name, workspace)

    def create_wmslayer(self, workspace, store, name, nativeName=None):
        # if not provided, fallback to name - this is what geoserver will do
        # anyway but nativeName needs to be provided if name is invalid xml
        # as this will cause verification errors since geoserver 2.6.1
        if nativeName is None:
            nativeName = name
        wms_url = store.href.replace('.xml', '/wmslayers')
        d = "<wmsLayer><name>{}</name><nativeName>{}</nativeName></wmsLayer>"
        data = d.format(name, nativeName)
        headers = {
            "Content-type": "text/xml",
            "Accept": "application/xml"
        }
        r = self.session.post(wms_url, data=data, headers=headers)
        self._cache.clear()
        status_code = r.status_code
        if status_code < 200 or status_code > 299:
            raise UploadError(r.text)
        return self.get_resource(name, store=store, workspace=workspace)

    def add_data_to_store(self, store, name, data, workspace=None,
                          overwrite=False, charset=None):
        """
        Add shapefile data to store.
        """
        if isinstance(store, str):
            store = self.get_store(store, workspace=workspace)
        if workspace is not None:
            workspace = _name(workspace)
            msg = "Specified store ({}) is not in specified workspace ({})!"
            msg = msg.format(store, workspace)
            assert store.workspace.name == workspace, msg
        else:
            workspace = store.workspace.name
        store = store.name
        if isinstance(data, dict):
            bundle = prepare_upload_bundle(name, data)
        else:
            bundle = data
        params = dict()
        if overwrite:
            params["update"] = "overwrite"
        if charset is not None:
            params["charset"] = charset
        headers = {
            'Content-Type': 'application/zip',
            'Accept': 'application/xml'
        }
        upload_url = urljoin(
            self.service_url,
            "workspaces/{}/datastores/{}/file.shp".format(
                workspace,
                store
            )
        )
        try:
            with open(bundle, "rb") as f:
                data = f.read()
                r = self.session.put(upload_url, params=params, data=data,
                                     headers=headers)
                self._cache.clear()
                if r.status_code != 201:
                    raise UploadError(r.text)
        finally:
            os.unlink(bundle)

    def create_featurestore(self, name, data, workspace=None, overwrite=False,
                            charset=None):
        """
        Create a shapefile datastore from a shapefile.
        """
        if not overwrite:
            try:
                self.get_store(name, workspace)
                msg = "There is already a store named " + name
                if workspace:
                    msg += " in " + str(workspace)
                raise ConflictingDataError(msg)
            except FailedRequestError:
                # we don't really expect that every layer name will be taken
                pass

        if workspace is None:
            workspace = self.get_default_workspace()
        workspace = _name(workspace)
        params = dict()
        if charset is not None:
            params['charset'] = charset
        ds_url = urljoin(
            self.service_url,
            "workspaces/{}/datastores/{}/file.shp".format(
                workspace, name
            )
        )
        # PUT /workspaces/<ws>/datastores/<ds>/file.shp
        headers = {
            "Content-type": "application/zip",
            "Accept": "application/xml"
        }
        if isinstance(data, dict):
            LOGGER.debug('Data is NOT a zipfile')
            archive = prepare_upload_bundle(name, data)
        else:
            LOGGER.debug('Data is a zipfile')
            archive = data
        message = open(archive, 'rb')
        try:
            r = self.session.put(ds_url, data=message, headers=headers,
                                 params=params)
            self._cache.clear()
            if r.status_code != 201:
                raise UploadError(r.text)
        finally:
            message.close()
            os.unlink(archive)

    def create_imagemosaic(self, name, data, configure=None, workspace=None,
                           overwrite=False, charset=None):
        if not overwrite:
            try:
                self.get_store(name, workspace)
                msg = "There is already a store named " + name
                if workspace:
                    msg += " in " + str(workspace)
                raise ConflictingDataError(msg)
            except FailedRequestError:
                # we don't really expect that every layer name will be taken
                pass

        if workspace is None:
            workspace = self.get_default_workspace()
        workspace = _name(workspace)
        params = dict()
        if charset is not None:
            params['charset'] = charset
        if configure is not None:
            params['configure'] = "none"
        cs_url = urljoin(
            self.service_url,
            "workspaces/{}/coveragestores/{}/file.imagemosaic".format(
                workspace, name
            )
        )
        # PUT /workspaces/<ws>/coveragestores/<name>/file.imagemosaic?configure=none
        headers = {
            "Content-type": "application/zip",
            "Accept": "application/xml"
        }
        if isinstance(data, str):
            message = open(data, 'rb')
        else:
            message = data
        try:
            r = self.session.put(cs_url, data=message, headers=headers,
                                 params=params)
            self._cache.clear()
            if r.status_code != 201:
                raise UploadError(r.text)
        finally:
            if hasattr(message, "close"):
                message.close()

    def create_coveragestore(self, name, data, workspace=None,
                             overwrite=False):
        self._create_coveragestore(name, data, workspace, overwrite)

    def create_coveragestore_external_geotiff(self, name, data, workspace=None,
                                              overwrite=False):
        self._create_coveragestore(name, data, workspace=workspace,
                                   overwrite=overwrite, external=True)

    def _create_coveragestore(self, name, data, workspace=None,
                              overwrite=False, external=False):
        if not overwrite:
            try:
                _ = self.get_store(name, workspace)
                msg = "There is already a store named " + name
                if workspace:
                    msg += " in " + str(workspace)
                raise ConflictingDataError(msg)
            except FailedRequestError:
                # we don't really expect that every layer name will be taken
                pass

        if workspace is None:
            workspace = self.get_default_workspace()

        archive = None
        ext = "geotiff"
        content_type = "image/tiff" if not external else "text/plain"
        store_type = "file." if not external else "external."

        headers = {
            "Content-type": content_type,
            "Accept": "application/xml"
        }

        message = data
        if not external:
            if isinstance(data, dict):
                archive = prepare_upload_bundle(name, data)
                message = open(archive, 'rb')
                if "tfw" in data:
                    # If application/archive was used, server crashes with
                    # a 500 error read in many sites that application/zip
                    # will do the trick. Successfully tested
                    headers['Content-type'] = 'application/zip'
                    ext = "worldimage"
            elif isinstance(data, str):
                message = open(data, 'rb')
            else:
                message = data

        cs_url = urljoin(
            self.service_url,
            "workspaces/{}/coveragestores/{}/{}{}".format(
                workspace.name, name, store_type, ext
            )
        )
        params = {"configure": "first", "coverageName": name}

        try:
            r = self.session.put(cs_url, data=message, headers=headers,
                                 params=params)
            self._cache.clear()
            if r.status_code != 201:
                raise UploadError(r.text)
        finally:
            if hasattr(message, "close"):
                message.close()
            if archive is not None:
                os.unlink(archive)

    def harvest_externalgranule(self, data, store):
        """
        Harvest a granule into an existing imagemosaic
        """
        params = dict()
        cs_url = urljoin(
            self.service_url,
            "workspaces/{}/coveragestores/{}/external.imagemosaic".format(
                store.workspace.name,
                store.name
            )
        )
        # POST /workspaces/<ws>/coveragestores/<name>/external.imagemosaic
        headers = {
            "Content-type": "text/plain",
            "Accept": "application/xml"
        }
        r = self.session.post(cs_url, data=data,
                              headers=headers, params=params)
        self._cache.clear()
        if r.status_code != 202:
            raise UploadError(r.text)

    def harvest_uploadgranule(self, data, store):
        """
        Harvest a granule into an existing imagemosaic
        Difference with harvest_externalgranule?
        """
        params = dict()
        cs_url = urljoin(
            self.service_url,
            "workspaces/{}/coveragestores/{}/file.imagemosaic".format(
                store.workspace.name,
                store.name
            )
        )
        # POST /workspaces/<ws>/coveragestores/<name>/file.imagemosaic
        headers = {
            "Content-type": "application/zip",
            "Accept": "application/xml"
        }
        message = open(data, 'rb')
        try:
            r = self.session.post(cs_url, data=message,
                                  headers=headers, params=params)
            self._cache.clear()
            if r.status_code != 202:
                raise UploadError(r.text)
        finally:
            if hasattr(message, "close"):
                message.close()

    def mosaic_coverages(self, store):
        """
        Print granules of an existing imagemosaic
        """
        params = dict()
        cs_url = urljoin(
            self.service_url,
            "workspaces/{}/coveragestores/{}/coverages.json".format(
                store.workspace.name,
                store.name
            )
        )
        # GET /workspaces/<ws>/coveragestores/<name>/coverages.json
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json"
        }
        r = self.session.get(cs_url, headers=headers, params=params)
        self._cache.clear()
        coverages = json.loads(r.text, object_hook=_decode_dict)
        return coverages

    def mosaic_coverage_schema(self, coverage, store):
        """
        Print granules of an existing imagemosaic
        """
        params = dict()
        cs_url = urljoin(
            self.service_url,
            "workspaces/{}/coveragestores/{}/coverages/{}/index.json".format(
                store.workspace.name,
                store.name,
                coverage
            )
        )
        # GET /workspaces/<ws>/coveragestores/<name>/coverages/<coverage>/index.json
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json"
        }
        r = self.session.get(cs_url, headers=headers, params=params)
        self._cache.clear()
        schema = json.loads(r.text, object_hook=_decode_dict)
        return schema

    def mosaic_granules(self, coverage, store, filter_=None, limit=None,
                        offset=None):
        """
        Print granules of an existing imagemosaic
        """
        params = dict()
        if filter_ is not None:
            params['filter'] = filter_
        if limit is not None:
            params['limit'] = limit
        if offset is not None:
            params['offset'] = offset
        p = "workspaces/{}/coveragestores/{}/coverages/{}/index/granules.json"
        cs_url = urljoin(
            self.service_url,
            p.format(store.workspace.name, store.name, coverage)
        )
        # GET /workspaces/<ws>/coveragestores/<name>/coverages/<coverage>/index/granules.json
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json"
        }
        r = self.session.get(cs_url, headers=headers, params=params)
        self._cache.clear()
        granules = json.loads(r.text, object_hook=_decode_dict)
        return granules

    def mosaic_delete_granule(self, coverage, store, granule_id):
        """
        Deletes a granule of an existing imagemosaic
        """
        params = dict()
        p = "workspaces/{}/coveragestores/{}/coverages/{}"\
            + "/index/granules/{}.json"
        cs_url = urljoin(
            self.service_url,
            p.format(store.workspace.name, store.name, coverage, granule_id)
        )
        # DELETE /workspaces/<ws>/coveragestores/<name>/coverages/<coverage>/index/granules/<granule_id>.json
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json"
        }
        r = self.session.delete(cs_url, headers=headers, params=params)
        self._cache.clear()
        if r.status_code != 200:
            raise FailedRequestError(r.text)

    def publish_featuretype(self, name, store, native_crs, srs=None,
                            jdbc_virtual_table=None, native_bbox=None):
        """
        Publish a featuretype from data in an existing store
        """
        # @todo native_srs doesn't seem to get detected, even when in the DB
        # metadata (at least for postgis in geometry_columns) and then there
        # will be a misconfigured layer
        if native_crs is None:
            raise ValueError("must specify native_crs")
        srs = srs or native_crs
        feature_type = FeatureType(self, store.workspace, store, name)
        # because name is the in FeatureType base class, work around that
        # and hack in these others that don't have xml properties
        feature_type.dirty['name'] = name
        feature_type.dirty['srs'] = srs
        feature_type.dirty['nativeCRS'] = native_crs
        if native_bbox is not None:
            feature_type.native_bbox = native_bbox
        feature_type.enabled = True
        feature_type.title = name
        headers = {
            "Content-type": "application/xml",
            "Accept": "application/xml"
        }
        resource_url = store.resource_url
        params = dict()
        if jdbc_virtual_table is not None:
            feature_type.metadata = ({
                'JDBC_VIRTUAL_TABLE': jdbc_virtual_table
            })
            resource_url = urljoin(
                self.service_url,
                "workspaces/{}/datastores/{}/featuretypes.json".format(
                    store.workspace.name,
                    store.name
                )
            )
        # What is the use of this request?
        r = self.session.post(resource_url, data=feature_type.message(),
                              headers=headers, params=params)
        if r.status_code < 200 or r.status_code > 299:
            raise UploadError(r.text)
        feature_type.fetch()
        return feature_type

    def get_resource(self, name, store=None, workspace=None):
        if store is not None and workspace is not None:
            if isinstance(workspace, str):
                workspace = self.get_workspace(workspace)
            if isinstance(store, str):
                store = self.get_store(store, workspace)
            if store is not None:
                return store.get_resources(name)

        if store is not None:
            candids = [s for s in self.get_resources(store) if s.name == name]
            if len(candids) == 0:
                return None
            elif len(candids) > 1:
                raise AmbiguousRequestError
            else:
                return candids[0]

        if workspace is not None:
            for store in self.get_stores(workspace):
                resource = self.get_resource(name, store)
                if resource is not None:
                    return resource
            return None

        for ws in self.get_workspaces():
            resource = self.get_resource(name, workspace=ws)
            if resource is not None:
                return resource
        return None

    def get_resource_by_url(self, url):
        xml = self.get_xml(url)
        name = xml.find("name").text
        if xml.tag == 'featureType':
            resource = FeatureType
        elif xml.tag == 'coverage':
            resource = Coverage
        else:
            raise Exception('drat')
        return resource(self, None, None, name, href=url)

    def get_resources(self, store=None, workspace=None):
        if isinstance(workspace, str):
            workspace = self.get_workspace(workspace)
        if isinstance(store, str):
            store = self.get_store(store, workspace)
        if store is not None:
            return store.get_resources()
        if workspace is not None:
            resources = []
            for store in self.get_stores(workspace):
                resources.extend(self.get_resources(store))
            return resources
        resources = []
        for ws in self.get_workspaces():
            resources.extend(self.get_resources(workspace=ws))
        return resources

    def get_layer(self, name):
        try:
            layer = Layer(self, name)
            layer.fetch()
            return layer
        except FailedRequestError:
            return None

    def get_layers(self, resource=None):
        if isinstance(resource, str):
            resource = self.get_resource(resource)
        layers_url = urljoin(
            self.service_url,
            "layers.xml"
        )
        description = self.get_xml(layers_url)
        layers = [Layer(self, l.find("name").text)
                  for l in description.findall("layer")]
        if resource is not None:
            layers = [l for l in layers if l.resource.href == resource.href]
        # TODO: Filter by style
        return layers

    def get_layergroup(self, name=None, workspace=None):
        try:
            path_parts = "layergroups/{}.xml".format(name)
            if workspace is not None:
                wks_name = _name(workspace)
                path_parts = "workspaces/{}".format(wks_name) + path_parts
            group_url = urljoin(
                self.service_url,
                path_parts
            )
            group = self.get_xml(group_url)
            if group.find("workspace"):
                wks_name = group.find("workspace").find("name").text
            else:
                wks_name = None
            return LayerGroup(self, group.find("name").text, wks_name)
        except FailedRequestError:
            return None

    def get_layergroups(self, workspace=None):
        wks_name = None
        path_parts = 'layergroups.xml'
        if workspace is not None:
            wks_name = _name(workspace)
            path_parts = 'workspaces/{}/{}'.format(wks_name, path_parts)
        groups_url = urljoin(
            self.service_url,
            path_parts
        )
        groups = self.get_xml(groups_url)
        return [LayerGroup(self, g.find("name").text, wks_name)
                for g in groups.findall("layerGroup")]

    def create_layergroup(self, name, layers=(), styles=(), bounds=None,
                          abstract=None, title=None, workspace=None):
        if any(g.name == name for g in self.get_layergroups()):
            msg = "LayerGroup named {} already exists!"
            raise ConflictingDataError(msg.format(name))
        else:
            return UnsavedLayerGroup(self, name, layers, styles, bounds,
                                     abstract, title, workspace)

    def get_style(self, name, workspace=None):
        """
        Find a Style in the catalog if one exists that matches the given name.
        If name is fully qualified in the form of `workspace:name` the workspace
        may be ommitted.

        :param name: name of the style to find
        :param workspace: optional workspace to search in
        """
        if ':' in name:
            workspace, name = name.split(':', 1)
        try:
            style = Style(self, name, _name(workspace))
            style.fetch()
        except FailedRequestError:
            style = None
        return style

    def get_style_by_url(self, style_workspace_url):
        try:
            dom = self.get_xml(style_workspace_url)
        except FailedRequestError:
            return None
        rest_parts = style_workspace_url.replace(self.service_url, '')\
            .split('/')
        # check for /workspaces/<ws>/styles/<stylename>
        workspace = None
        if 'workspaces' in rest_parts:
            workspace = rest_parts[rest_parts.index('workspaces') + 1]
        return Style(self, dom.find("name").text, workspace)

    def get_styles(self, workspace=None):
        styles_xml = "styles.xml"
        if workspace is not None:
            styles_xml = "workspaces/{0}/styles.xml".format(_name(workspace))
        styles_url = urljoin(
            self.service_url,
            styles_xml
        )
        description = self.get_xml(styles_url)
        return [Style(self, s.find('name').text)
                for s in description.findall("style")]

    def create_style(self, name, data, overwrite=False, workspace=None,
                     style_format="sld10", raw=False):
        style = self.get_style(name, workspace)
        if not overwrite and style is not None:
            msg = "There is already a style named {}".format(name)
            raise ConflictingDataError(msg)
        if not overwrite or style is None:
            headers = {
                "Content-type": "text/xml",
            }
            xml = "<style><name>{0}</name><filename>{0}.sld"\
                  + "</filename></style>"
            xml = xml.format(name)
            style = Style(self, name, workspace, style_format)
            r = self.session.post(style.create_href, data=xml, headers=headers)
            if r.status_code < 200 or r.status_code > 299:
                raise UploadError(r.text+" "+str(r.status_code))
        headers = {
            "Content-type": style.content_type,
            "Accept": "application/xml"
        }
        body_href = style.body_href
        if raw:
            body_href += "?raw=true"
        r = self.session.put(body_href, data=data, headers=headers)
        if r.status_code < 200 or r.status_code > 299:
            raise UploadError(r.text)
        self._cache.pop(style.href, None)
        self._cache.pop(style.body_href, None)

    def create_workspace(self, name):
        xml = "<workspace><name>{name}</name></workspace>"\
            .format(name=name)
        headers = {"Content-Type": "application/xml"}
        workspace_url = urljoin(
            self.service_url,
            "workspaces/"
        )
        r = self.session.post(workspace_url, data=xml, headers=headers)
        assert 200 <= r.status_code < 300,\
            "Tried to create workspace but got {}: {}".format(r.status_code,
                                                              r.text)
        self._cache.pop(urljoin(self.service_url, "workspaces.xml"), None)
        return self.get_workspace(name)

    def get_workspaces(self):
        rest_url = urljoin(
            self.service_url,
            "workspaces.xml"
        )
        description = self.get_xml(rest_url)
        return [workspace_from_index(self, node)
                for node in description.findall("workspace")]

    def get_workspace(self, name):
        candidates = [w for w in self.get_workspaces() if w.name == name]
        if len(candidates) == 0:
            return None
        elif len(candidates) > 1:
            raise AmbiguousRequestError()
        else:
            return candidates[0]

    def get_default_workspace(self):
        ws = Workspace(self, "default")
        # must fetch and resolve the 'real' workspace from the response
        ws.fetch()
        return workspace_from_index(self, ws.dom)

    def set_default_workspace(self, name):
        if hasattr(name, 'name'):
            name = name.name
        workspace = self.get_workspace(name)
        if workspace is not None:
            headers = {"Content-Type": "application/xml"}
            default_workspace_url = urljoin(
                self.service_url,
                "workspaces/default.xml"
            )
            msg = "<workspace><name>{}</name></workspace>".format(name)
            r = self.session.put(default_workspace_url, data=msg,
                                 headers=headers)
            assert 200 <= r.status_code < 300,\
                "Error setting default workspace: {}: {}"\
                .format(r.status_code, r.text)
            self._cache.pop(default_workspace_url, None)
            self._cache.pop("{}/workspaces.xml".format(self.service_url), None)
        else:
            raise FailedRequestError("no workspace named '{}'".format(name))


def _name(named):
    """
    Get the name out of an object.  This varies based on the type of the input:
       * the "name" of a string is itself
       * the "name" of None is itself
       * the "name" of an object with a property named name is that property -
         as long as it's a string
       * otherwise, we raise a ValueError
    """
    if isinstance(named, str) or named is None:
        return named
    elif hasattr(named, 'name') and isinstance(named.name, str):
        return named.name
    else:
        msg = "Can't interpret {} as a name or a configuration object"
        raise ValueError(msg.format(named))


class UploadError(Exception):
    pass


class ConflictingDataError(Exception):
    pass


class AmbiguousRequestError(Exception):
    pass


class FailedRequestError(Exception):
    pass
