#!/usr/bin/env python3

import itertools
import os
import sys
import time
import re
import json

import attr
import backoff
import pendulum
import requests
import dateutil.parser
import singer
import singer.metrics as metrics
from singer import utils, metadata
from singer import (UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING,
                    _transform_datetime)


class SourceUnavailableException(Exception):
    '''Exception for source unavailable'''
    pass


REQUIRED_CONFIG_KEYS = ["api_key", "start_date"]
PER_PAGE = 100
BASE_URL = "https://api2.autopilothq.com/v1"
CONFIG = {
    "api_key": None,
    "start_date": None,

    # Optional
    "user_agent": None
}


LOGGER = singer.get_logger()
SESSION = requests.session()


ENDPOINTS = {
    "contacts":                "/contacts",
    "custom_fields":           "/contacts/custom_fields",
    "lists":                   "/lists",
    "smart_segments":          "/smart_segments",
    "smart_segments_contacts": "/smart_segments/{segment_id}/contacts",
}


def get_abs_path(path):
    '''Returns the absolute path'''
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)

def load_schema(entity):
    '''Returns the schema for the specified source'''
    schema = utils.load_json(get_abs_path("schemas/{}.json".format(entity)))

    return schema


def get_start(STATE, tap_stream_id, bookmark_key):
    current_bookmark = singer.get_bookmark(STATE, tap_stream_id, bookmark_key)
    if current_bookmark is None:
        return CONFIG["start_date"]

    return current_bookmark

def client_error(exc):
    '''Indicates whether the given RequestException is a 4xx response'''
    return exc.response is not None and exc.response.status_code != 408 and 400 <= exc.response.status_code < 500


def parse_source_from_url(url):
    '''Given an Autopilot URL, extract the source name (e.g. "contacts")'''
    url_regex = re.compile(BASE_URL +  r'.*/(\w+)')
    match = url_regex.match(url)

    if match:
        if match.group(1) == "contacts":
            if "segment" in match.group(0):
                return "smart_segments_contacts"
        return match.group(1)

    raise ValueError("Can't determine stream from URL " + url)


def parse_key_from_source(source):
    '''Given an Autopilot source, return the key needed to access the children
       The endpoints for fetching contacts related to a list or segment
       have the contacts in a child with the key of contacts
    '''
    if 'contact' in source:
        return 'contacts'

    elif 'smart_segments' in source:
        return 'segments'

    return source


def transform_contact(contact):
    '''Transform the properties on a contact
    to be more database friendly

    Do this explicitly for the boolean and timestamp props
    '''
    boolean_props = ["anywhere_page_visits", "anywhere_form_submits", "anywhere_utm"]
    timestamp_props = ["mail_received", "mail_opened", "mail_clicked", "mail_bounced", "mail_complained", "mail_unsubscribed", "mail_hardbounced"]

    for prop in boolean_props:
        if prop in contact:
            formatted_array = []
            for row in contact[prop]:
                formatted_array.append({
                    "url": row,
                    "value": contact[prop][row]
                })
            contact[prop] = formatted_array

    for prop in timestamp_props:
        if prop in contact:
            formatted_array = []
            for row in contact[prop]:
                formatted_array.append({
                    "id": row,
                    "timestamp": _transform_datetime(
                        (contact[prop][row]),
                        UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING)
                })
            contact[prop] = formatted_array

    return contact


def get_url(endpoint, **kwargs):
    '''Get the full url for the endpoint'''
    if endpoint not in ENDPOINTS:
        raise ValueError("Invalid endpoint {}".format(endpoint))


    return BASE_URL + ENDPOINTS[endpoint].format(**kwargs)


@backoff.on_exception(backoff.expo,
                      (requests.exceptions.RequestException),
                      max_tries=5,
                      giveup=client_error,
                      factor=2)
@utils.ratelimit(20, 1)
def request(url, params=None):
    '''Make a request to the given Autopilot URL.
    Appends Autopilot API bookmark to url if in params

    Handles retrying, rate-limiting and status checking.
    Logs request duration and records per second
    '''
    headers = {"autopilotapikey": CONFIG["api_key"]}

    if "user_agent" in CONFIG and CONFIG["user_agent"] is not None:
        headers["user-agent"] = CONFIG["user_agent"]

    if params and "bookmark" in params:
        url = url + "/" + params["bookmark"]

    req = requests.Request("GET", url, headers=headers).prepare()
    LOGGER.info("GET %s", req.url)

    with metrics.http_request_timer(parse_source_from_url(url)) as timer:
        resp = SESSION.send(req)
        timer.tags[metrics.Tag.http_status_code] = resp.status_code
        resp.raise_for_status()
        return resp


def gen_request(endpoint, params=None):
    '''Generate a request that will iterate through the results
    and paginate through the responses until the amount of results
    returned is less than 100, the amount returned by the API.

    If the source has 'contact' in it, Autopilot API will provide a
    'bookmark' property at the top level that is used to paginate
    results



    The API only returns bookmarks for iterating through contacts
    '''
    params = params or {}

    source = parse_source_from_url(endpoint)
    source_key = parse_key_from_source(source)

    with metrics.record_counter(source) as counter:
        while True:
            data = request(endpoint, params).json()
            if 'contact' in source:
                if "bookmark" in data:
                    params["bookmark"] = data["bookmark"]

                else:
                    params = {}

            for row in data[source_key]:
                counter.increment()
                yield row

            if len(data[source_key]) < PER_PAGE:
                params = {}
                break


def sync_contacts(STATE, stream):
    '''Sync contacts from the Autopilot API

    The API returns data in the following format

    {
        "contacts": [{...},{...}],
        "total_contacts": 400,
        "bookmark": "person_9EAF39E4-9AEC-4134-964A-D9D8D54162E7"
    }

    Params:
    STATE - State dictionary
    stream - Stream dictionary from the catalog
    '''
    mdata = metadata.to_map(stream['metadata'])
    tap_stream_id = stream['tap_stream_id']
    singer.write_schema(tap_stream_id,
                        stream['schema'],
                        metadata.get(mdata, (), 'table-key-properties'))

    start = utils.strptime_with_tz(get_start(STATE, tap_stream_id, "updated_at"))

    LOGGER.info("Only syncing contacts updated since " + utils.strftime(start))
    max_updated_at = start

    for row in gen_request(get_url(tap_stream_id)):
        updated_at = None
        if "updated_at" in row:
            updated_at = utils.strptime_with_tz(
                _transform_datetime( # pylint: disable=protected-access
                    row["updated_at"],
                    UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING))

        if not updated_at or updated_at >= start:
            singer.write_record(tap_stream_id, transform_contact(row))

        if updated_at and updated_at > max_updated_at:
            max_updated_at = updated_at

    STATE = singer.write_bookmark(STATE, tap_stream_id, "updated_at", utils.strftime(max_updated_at))
    singer.write_state(STATE)

    LOGGER.info("Completed Contacts Sync")
    return STATE


def sync_lists(stream):
    '''Sync all lists from the Autopilot API

    The API returns data in the following format

    {
        "lists": [
            {
            "list_id": "contactlist_06444749-9C0F-4894-9A23-D6872F9B6EF8",
            "title": "1k.csv"
            },
            {
            "list_id": "contactlist_0FBA1FA2-5A12-413B-B1A8-D113E6B3CDA8",
            "title": "____NEW____"
            }
        ]
     }

    '''
    singer.write_schema("lists", stream['schema'], ["list_id"])

    for row in gen_request(get_url("lists")):
        singer.write_record("lists", row)

    LOGGER.info("Completed Lists Sync")


def sync_smart_segments(stream):
    '''Sync all smart segments from the Autopilot API

    The API returns data in the following format

    {
        "segments": [
            {
            "segment_id": "contactlist_sseg1456891025207",
            "title": "Ladies"
            },
            {
            "segment_id": "contactlist_sseg1457059448884",
            "title": "Gentlemen"
            }
        ]
    }

    '''
    singer.write_schema("smart_segments", stream['schema'], ["segment_id"])

    for row in gen_request(get_url("smart_segments")):
        singer.write_record("smart_segments", row)

    LOGGER.info("Completed Smart Segments Sync")


def sync_smart_segment_contacts(stream):
    '''Sync the contacts on a given smart segment from the Autopilot API

    {
        "contacts": [{...},{...}],
        "total_contacts": 2
    }
    '''
    singer.write_schema(
        "smart_segments_contacts",
        stream['schema'],
        ["segment_id", "contact_id"])

    for row in gen_request(get_url("smart_segments")):
        subrow_url = get_url("smart_segments_contacts", segment_id=row["segment_id"])
        for subrow in gen_request(subrow_url):
            singer.write_record("smart_segments_contacts", {
                "segment_id": row["segment_id"],
                "contact_id": subrow["contact_id"]
            })

    LOGGER.info("Completed Smart Segments Contacts Sync")


# {tap_stream_id: [key_properties]}
STREAMS = {
    "contacts" : ["contact_id"],
    "lists": ["list_id"],
    "smart_segments": ["segment_id"],
    "smart_segments_contacts": ["segment_id", "contact_id"]
}


def get_streams_to_sync(streams, state):
    '''Get the streams to sync'''
    current_stream = singer.get_currently_syncing(state)
    result = streams
    if current_stream:
        result = list(itertools.dropwhile(lambda x: x.get('tap_stream_id') != current_stream,
                                          streams))
    if not result:
        raise Exception("Unknown stream {} in state".format(current_stream))
    return result


def get_selected_streams(remaining_streams):
    selected_streams = []

    for stream in remaining_streams:
        mdata = metadata.to_map(stream.get('metadata'))

        if metadata.get(mdata, (), 'selected') == True:
            selected_streams.append(stream)
        else:
            singer.log_info("%s: not selected", stream["tap_stream_id"])

    return selected_streams

def sync(state, stream):
    return_val = state

    if stream['tap_stream_id'] == 'contacts':
        return_val = sync_contacts(state, stream)
    elif stream['tap_stream_id'] == 'lists':
        sync_lists(stream)
    elif stream['tap_stream_id'] == 'smart_segments':
        sync_smart_segments(stream)
    elif stream['tap_stream_id'] == 'smart_segments_contacts':
        sync_smart_segment_contacts(stream)

    return return_val

def do_sync(STATE, catalog):
    '''Sync the streams that were selected'''
    remaining_streams = get_streams_to_sync(catalog['streams'], STATE)
    selected_streams = get_selected_streams(remaining_streams)

    if len(selected_streams) < 1:
        LOGGER.info("No Streams selected, please check that you have a schema selected in your catalog")
        return

    LOGGER.info("Starting sync. Will sync these streams: %s",
                [stream['tap_stream_id'] for stream in selected_streams])

    for stream in selected_streams:
        LOGGER.info("Syncing %s", stream['tap_stream_id'])
        singer.set_currently_syncing(STATE, stream['tap_stream_id'])
        singer.write_state(STATE)

        STATE = sync(STATE, stream)

    singer.set_currently_syncing(STATE, None)
    singer.write_state(STATE)
    LOGGER.info("Sync completed")


def discover_schemas():
    '''Iterate through streams, push to an array and return'''
    result = {'streams': []}
    for tap_stream_id, key_properties in STREAMS.items():
        LOGGER.info('Loading schema for %s', tap_stream_id)
        schema = load_schema(tap_stream_id)

        mdata = metadata.new()
        mdata = metadata.write(mdata, (), 'table-key-properties', key_properties)

        if tap_stream_id == 'contacts':
            mdata = metadata.write(mdata, (), 'valid-replication-keys', ['updated_at'])

        for field_name in schema['properties'].keys():
            mdata = metadata.write(mdata, ('properties', field_name), 'inclusion', 'automatic')

        result['streams'].append({
            'stream': tap_stream_id,
            'tap_stream_id': tap_stream_id,
            'schema': schema,
            'metadata': metadata.to_list(mdata)
        })

    return result


def do_discover():
    '''JSON dump the schemas to stdout'''
    LOGGER.info("Loading Schemas")
    json.dump(discover_schemas(), sys.stdout, indent=4)


def main():
    '''Entry point'''
    args = utils.parse_args(REQUIRED_CONFIG_KEYS)

    CONFIG.update(args.config)
    STATE = {}

    if args.state:
        STATE.update(args.state)

    if args.discover:
        do_discover()
    elif args.properties:
        do_sync(STATE, args.properties)
    else:
        LOGGER.info("No Streams were selected")


if __name__ == "__main__":
    main()
