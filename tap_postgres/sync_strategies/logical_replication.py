#!/usr/bin/env python3
import singer
import decimal
import datetime
import dateutil.parser
from singer import utils, write_message, get_bookmark
import singer.metadata as metadata
import singer.metrics as metrics
from singer.schema import Schema
import tap_postgres.db as post_db
import psycopg2
import copy
import pdb
import pytz
import time
from select import select
import json

LOGGER = singer.get_logger()

UPDATE_BOOKMARK_PERIOD = 1000

def fetch_current_lsn(connection):
   with  connection.cursor() as cur:
      cur.execute("SELECT pg_current_xlog_location();")
      current_scn = cur.fetchone()[0]
      file, index =  current_scn.split('/')
      return int('ff000000',16) * int(file,16) + int(index,16)

def add_automatic_properties(stream):
   stream.schema.properties['_sdc_deleted_at'] = Schema(
            type=['null', 'string'], format='date-time')

def get_stream_version(tap_stream_id, state):
   stream_version = singer.get_bookmark(state, tap_stream_id, 'version')

   if stream_version is None:
      raise Exception("version not found for log miner {}".format(tap_stream_id))

   return stream_version

def row_to_singer_message(stream, row, version, columns, time_extracted):
    row_to_persist = ()
    for idx, elem in enumerate(row):
        property_type = stream.schema.properties[columns[idx]].type
        multiple_of = stream.schema.properties[columns[idx]].multipleOf
        format = stream.schema.properties[columns[idx]].format #date-time
        if elem is None:
            row_to_persist += (elem,)
        elif 'integer' in property_type or property_type == 'integer':
            integer_representation = int(elem)
            row_to_persist += (integer_representation,)
        elif ('number' in property_type or property_type == 'number') and multiple_of:
            decimal_representation = decimal.Decimal(elem)
            row_to_persist += (decimal_representation,)
        elif ('number' in property_type or property_type == 'number'):
            row_to_persist += (float(elem),)
        elif format == 'date-time':
            row_to_persist += (elem,)
        else:
            row_to_persist += (elem,)

    rec = dict(zip(columns, row_to_persist))
    return singer.RecordMessage(
        stream=stream.stream,
        record=rec,
        version=version,
        time_extracted=time_extracted)

def consume_message(stream, state, msg, time_extracted):
   payload = json.loads(msg.payload)
   lsn = msg.data_start
   stream_version = get_stream_version(stream.tap_stream_id, state)

   # pdb.set_trace()
   for c in payload['change']:
      if c['kind'] == 'insert':
         col_vals  = c['columnvalues'] + [None]
         col_names = c['columnnames'] + ['_sdc_deleted_at']
         record_message = row_to_singer_message(stream, col_vals, stream_version, col_names, time_extracted)
      elif c['kind'] == 'update':
         col_vals  = c['columnvalues'] + [None]
         col_names = c['columnnames'] + ['_sdc_deleted_at']
         record_message = row_to_singer_message(stream, col_vals, stream_version, col_names, time_extracted)
      elif c['kind'] == 'delete':
         col_names = c['oldkeys']['keynames'] + ['_sdc_deleted_at']
         col_vals  = c['oldkeys']['keyvalues']  + [singer.utils.strftime(time_extracted)]
         record_message = row_to_singer_message(stream, col_vals, stream_version, col_names, time_extracted)
      else:
         raise Exception("unrecognized replication operation: {}".format(c['kind']))

      singer.write_message(record_message)

      # LOGGER.info('RING3: record: %s with lsn %s', c, lsn)
      state = singer.write_bookmark(state,
                                    stream.tap_stream_id,
                                    'lsn',
                                    lsn)
      msg.cursor.send_feedback(flush_lsn=msg.data_start)

   return state

def sync_table(rep_conn, connection, stream, state, desired_columns):
   end_lsn = fetch_current_lsn(connection)
   time_extracted = utils.now()

   with rep_conn.cursor() as cur:
      LOGGER.info("starting Logical Replication")
      try:
         # test_decoding produces textual output
         cur.start_replication(slot_name='stitch', decode=True, start_lsn=get_bookmark(state, stream.tap_stream_id, 'lsn'))
      except psycopg2.ProgrammingError:
         cur.create_replication_slot('stitch', output_plugin='wal2json')
         # cur.create_replication_slot('stitch', output_plugin='test_decoding')
         cur.start_replication(slot_name='stitch', decode=True, start_lsn=get_bookmark(state, stream.tap_stream_id, 'lsn'))

      keepalive_interval = 10.0
      rows_saved = 0

      while True:
         LOGGER.info('reading message')
         msg = cur.read_message()
         if msg:
            state = consume_message(stream, state, msg, time_extracted)
            rows_saved = rows_saved + 1

            if rows_saved % UPDATE_BOOKMARK_PERIOD == 0:
               singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

         else:
            now = datetime.datetime.now()
            timeout = keepalive_interval - (now - cur.io_timestamp).total_seconds()
            try:
               sel = select([cur], [], [], max(0, timeout))
               if not any(sel):
                  break
            except InterruptedError:
               pass  # recalculate timeout and continue

   singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))
   return state
