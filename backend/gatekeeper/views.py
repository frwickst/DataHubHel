import cgi
import json
from collections import OrderedDict
from wsgiref.util import _hoppish

import requests
from boltons.iterutils import remap
from django.conf import settings
from django.contrib.auth import authenticate
from django.http import HttpResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from guardian.shortcuts import assign_perm
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Datastream, Thing
from .utils import parse_sta_url


class Gatekeeper(APIView):
    sts_self_base_url = None
    sts_url = None
    sts_headers = {}
    sts_arguments = {}
    sts_content_type = ''
    dumped_request_data= None

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        path = self.kwargs.pop('path')

        self.sts_self_base_url = request.build_absolute_uri(reverse('gatekeeper:index', kwargs={
            'path': ''
        })).rstrip('/')

        # TODO: validate path
        self.sts_url = '{}/{}'.format(settings.GATEKEEPER_STS_BASE_URL, path)

        self.sts_arguments['headers'] = self.filter_valid_sts_headers(request)

    def filter_valid_sts_headers(self, request):
        headers = {
            'Content-Type': request.META['CONTENT_TYPE'],
        }

        for header in request.META:
            if not header.startswith('HTTP_'):
                continue

            header_name = header[5:]
            if header_name not in ['ACCEPT', 'EXPECT', 'USER_AGENT']:
                continue

            headers[header_name] = request.META[header]
        return headers

    def get(self, request):
        self.sts_arguments['params'] = {k: v for k, v in request.query_params.items() if k.startswith('$')}
        return self.handle_request(request)

    def post(self, request):
        print("Data: \n", self.request.data)
        self.sts_arguments['json'] = self.request.data
        return self.handle_request(request)

    def put(self, request):
        self.sts_arguments['json'] = self.request.data
        return self.handle_request(request)

    def patch(self, request):
        self.sts_arguments['json'] = self.request.data
        return self.handle_request(request)

    def delete(self, request):
        self.sts_arguments['params'] = {k: v for k, v in request.query_params.items() if k.startswith('$')}
        return self.handle_request(request)

    def create(self, request, sts_response):
        # TODO: error checks
        entity_request = requests.get(sts_response.headers['location'])
        created_object_data = entity_request.json()

        if '@iot.id' in created_object_data and '@iot.selfLink' in created_object_data:
            self_link = created_object_data['@iot.selfLink'].replace(
                settings.GATEKEEPER_STS_BASE_URL, self.sts_self_base_url)

            # TODO: make prefix configurable
            parse_result = parse_sta_url(self_link, prefix='/api/v1.0')

            # Save the created entity to the local database
            if parse_result['type'] == 'entity' and parse_result['parts'][-1]['name'] == 'Thing':
                instance = Thing.objects.create(
                    sts_id=created_object_data.get('@iot.id'),
                    name=created_object_data.get('name'),
                    description=created_object_data.get('description')
                )

                instance.user = request.user
                instance.save()

                # Query datastreams and save them to the database
                if 'Datastreams@iot.navigationLink' in created_object_data:
                    # TODO: make version prefix configurable
                    datastreams_url = created_object_data['Datastreams@iot.navigationLink']
                    # TODO: error checks
                    datastreams_request = requests.get(datastreams_url)
                    datastreams_data = datastreams_request.json()
                    if datastreams_data.get('@iot.count', 0) > 0 and 'value' in datastreams_data:
                        for ds in datastreams_data.get('value'):
                            Datastream.objects.create(
                                sts_id=ds.get('@iot.id'),
                                name=ds.get('name'),
                                description=ds.get('description'),
                                user=request.user,
                                thing=instance,
                            )

            if parse_result['type'] == 'entity' and parse_result['parts'][-1]['name'] == 'Datastream':
                # Query the Thing this Datastream is a part of
                if 'Thing@iot.navigationLink' in created_object_data:
                    # TODO: make version prefix configurable
                    thing_url = '{}/v1.0/{}'.format(settings.GATEKEEPER_STS_BASE_URL,
                                                    created_object_data['Thing@iot.navigationLink'])
                    # TODO: error checks
                    thing_request = requests.get(thing_url)
                    thing_data = thing_request.json()

                    try:
                        thing = Thing.objects.get(sts_id=thing_data.get('@iot.id'))

                        instance = Datastream()
                        instance.thing = thing
                        instance.sts_id = created_object_data.get('@iot.id')
                        instance.name = created_object_data.get('name')
                        instance.description = created_object_data.get('description')

                        instance.user = request.user
                        instance.save()

                        assign_perm('subscribe_datastream', request.user, instance)
                        assign_perm('publish_datastream', request.user, instance)

                    except Thing.DoesNotExist:
                        # TODO: handle error
                        pass

    def handle_request(self, request):
        sts_response = requests.request(request.method, self.sts_url, **self.sts_arguments)
        status_code = sts_response.status_code
        content_type = sts_response.headers['Content-Type'] if 'Content-Type' in sts_response.headers else ''

        response = Response(content_type=content_type, status=sts_response.status_code)

        if status_code == 404 or status_code >= 500:
            response.data = sts_response.content
            return response

        if status_code == 201 and 'location' in sts_response.headers:
            self.create(request, sts_response)

        try:
            data = sts_response.json(object_pairs_hook=OrderedDict, encoding=sts_response.encoding)
            data = self.remap_response_content_urls(data)
            response.data = data
        except json.decoder.JSONDecodeError:
            response.data = sts_response.content

        headers = self.remap_response_headers(sts_response.headers)
        if headers:
            for name, value in headers.items():
                response[name] = value

        return response

    def remap_response_content_urls(self, data):
        def fix_urls(visit_path, key, value):
            if isinstance(key, str) and any([k in key for k in ['url', 'Link']]):
                return key, value.replace(settings.GATEKEEPER_STS_BASE_URL, self.sts_self_base_url)

            return key, value

        remapped_data = remap(data, visit=fix_urls)

        return remapped_data

    def remap_response_headers(self, headers):
        remapped_headers = {}

        for header_name in headers:
            if header_name.lower() in ['location', 'content-length'] or _hoppish(header_name.lower()):
                continue

            remapped_headers[header_name] = headers[header_name]

        # Rewrite location header url
        location_header = headers.get('location', None)
        if location_header and location_header.startswith(settings.GATEKEEPER_STS_BASE_URL):
            remapped_headers['Location'] = location_header.replace(settings.GATEKEEPER_STS_BASE_URL,
                                                                   self.sts_self_base_url)

        return remapped_headers
