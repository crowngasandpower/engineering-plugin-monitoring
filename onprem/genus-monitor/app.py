"""
Genus Monitoring Service — monitors Genus health, data freshness, and file processing.

Queries Genus SQL Server for connectivity and hub file freshness, and Crown MySQL
databases (EPS/GPS) for pending file imports that haven't been processed by Genus.

Required environment variables:
  GENUS_GAS_HOST, GENUS_GAS_DB, GENUS_GAS_USER, GENUS_GAS_PASSWORD
  GENUS_ELEC_HOST, GENUS_ELEC_DB, GENUS_ELEC_USER, GENUS_ELEC_PASSWORD (optional)
  MYSQL_HOST, MYSQL_USER, MYSQL_PASSWORD (for EPS/GPS genus_imports monitoring)
  PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD (for storing cross-reference data)

Optional:
  POLL_INTERVAL_SECONDS — how often to query (default: 300 = 5 minutes)
"""

import os
import time
import threading
import logging
from datetime import datetime

import json
import urllib.request
import ssl

import pyodbc
import pymysql
import psycopg2
import redis
from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST
from flask import Flask, Response

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL_SECONDS', '300'))

# --- Genus SQL Server connection config ---
GENUS_CONNECTIONS = {}

if os.environ.get('GENUS_GAS_HOST'):
    GENUS_CONNECTIONS['gas'] = {
        'host': os.environ['GENUS_GAS_HOST'],
        'database': os.environ['GENUS_GAS_DB'],
        'user': os.environ['GENUS_GAS_USER'],
        'password': os.environ['GENUS_GAS_PASSWORD'],
    }

if os.environ.get('GENUS_ELEC_HOST'):
    GENUS_CONNECTIONS['elec'] = {
        'host': os.environ['GENUS_ELEC_HOST'],
        'database': os.environ['GENUS_ELEC_DB'],
        'user': os.environ['GENUS_ELEC_USER'],
        'password': os.environ['GENUS_ELEC_PASSWORD'],
    }

# --- MySQL connection config ---
MYSQL_CONFIG = None
if os.environ.get('MYSQL_HOST'):
    MYSQL_CONFIG = {
        'host': os.environ['MYSQL_HOST'],
        'port': int(os.environ.get('MYSQL_PORT', '3306')),
        'user': os.environ['MYSQL_USER'],
        'password': os.environ['MYSQL_PASSWORD'],
    }

# --- PostgreSQL connection config ---
PG_CONFIG = None
if os.environ.get('PG_HOST'):
    PG_CONFIG = {
        'host': os.environ['PG_HOST'],
        'port': int(os.environ.get('PG_PORT', '5432')),
        'dbname': os.environ.get('PG_DB', 'codereview'),
        'user': os.environ.get('PG_USER', 'codereview'),
        'password': os.environ.get('PG_PASSWORD', 'codereview'),
    }

# --- Redis connection (EPS Horizon queue) ---
REDIS_CONFIG = None
if os.environ.get('EPS_REDIS_HOST'):
    REDIS_CONFIG = {
        'host': os.environ['EPS_REDIS_HOST'],
        'port': int(os.environ.get('EPS_REDIS_PORT', '6379')),
        'password': os.environ.get('EPS_REDIS_PASSWORD', ''),
        'prefix': os.environ.get('EPS_REDIS_PREFIX', 'eps_horizon:'),
    }

# --- PRTG connection config ---
PRTG_CONFIG = None
if os.environ.get('PRTG_URL'):
    PRTG_CONFIG = {
        'url': os.environ['PRTG_URL'].rstrip('/'),
        'apitoken': os.environ['PRTG_API_TOKEN'],
    }
    # Sensor IDs for temperature monitoring (iDRAC REST Custom v2 sensors)
    PRTG_TEMP_SENSORS = {
        2579: 'SVR1-R660',
        2581: 'SVR2-R660',
        2582: 'SVR3-R660',
        2583: 'SVR4-R540',
        2584: 'SVR5-R540',
        2585: 'SVR6-R540',
        2586: 'SVR7-R660',
    }

# --- Ubibot room sensors config ---
UBIBOT_SENSORS = []
for _idx in range(1, 10):
    _channel = os.environ.get(f'UBIBOT_{_idx}_CHANNEL_ID')
    if not _channel:
        continue
    _api_key = os.environ.get(f'UBIBOT_{_idx}_API_KEY')
    if not _api_key:
        logger.warning(f'UBIBOT_{_idx}_CHANNEL_ID is set but UBIBOT_{_idx}_API_KEY is missing — skipping sensor')
        continue
    UBIBOT_SENSORS.append({
        'channel_id': _channel,
        'api_key': _api_key,
        'name': os.environ.get(f'UBIBOT_{_idx}_NAME', _channel),
    })

# --- PowerStore SAN config ---
POWERSTORE_CONFIG = None
if os.environ.get('POWERSTORE_HOST'):
    POWERSTORE_CONFIG = {
        'host': os.environ['POWERSTORE_HOST'],
        'user': os.environ['POWERSTORE_USER'],
        'password': os.environ['POWERSTORE_PASSWORD'],
    }

# --- Prometheus metrics ---

# Genus connectivity
genus_up = Gauge('genus_up', 'Whether Genus DB is reachable (1=up, 0=down)', ['fuel'])
genus_query_duration = Gauge('genus_query_duration_seconds', 'Time taken to run monitoring queries', ['fuel'])

# Data freshness
genus_hub_file_latest_age_hours = Gauge('genus_hub_file_latest_age_hours',
    'Age of the most recent processed hub file in hours', ['fuel'])

# Replica lag
genus_replica_lag_seconds = Gauge('genus_replica_lag_seconds',
    'Replication lag in seconds (Always On Availability Group)', ['fuel'])

# File import pipeline — all derived from cross-reference
genus_imports_sent_today = Gauge('genus_imports_sent_today',
    'Files sent to Genus today (from MySQL)', ['app'])
genus_imports_processed_today = Gauge('genus_imports_processed_today',
    'Files sent today that Genus has processed (cross-referenced)', ['app'])
genus_imports_unprocessed = Gauge('genus_imports_unprocessed',
    'Files sent (last 7 days) not yet found in Genus tblHubFileFlows', ['app'])
genus_imports_oldest_unprocessed_minutes = Gauge('genus_imports_oldest_unprocessed_minutes',
    'Age in minutes of the oldest unprocessed file', ['app'])

# Welcome packs (ewelcome / ewelcome-power)
ewelcome_forms_created_today = Gauge('ewelcome_forms_created_today',
    'Welcome pack forms created today', ['fuel'])
ewelcome_forms_completed_today = Gauge('ewelcome_forms_completed_today',
    'Welcome pack forms completed today', ['fuel'])
ewelcome_forms_not_started = Gauge('ewelcome_forms_not_started',
    'Welcome pack forms not yet started (all time)', ['fuel'])
ewelcome_forms_downloaded = Gauge('ewelcome_forms_downloaded',
    'Welcome pack forms downloaded but not completed', ['fuel'])
ewelcome_failed_jobs = Gauge('ewelcome_failed_jobs',
    'Failed queue jobs', ['fuel'])
ewelcome_pending_jobs = Gauge('ewelcome_pending_jobs',
    'Pending queue jobs', ['fuel'])
ewelcome_completion_rate = Gauge('ewelcome_completion_rate',
    'Percentage of forms completed (all time)', ['fuel'])
ewelcome_completion_rate_today = Gauge('ewelcome_completion_rate_today',
    'Percentage of forms completed today', ['fuel'])

# EPS Pricing queue
eps_pricing_avg_seconds_hh = Gauge('eps_pricing_avg_seconds_hh',
    'Average time to price today for HH quotes (seconds)')
eps_pricing_avg_seconds_nhh = Gauge('eps_pricing_avg_seconds_nhh',
    'Average time to price today for non-HH quotes (seconds)')
eps_pricing_last_seconds_hh = Gauge('eps_pricing_last_seconds_hh',
    'Time to price the most recently priced HH quote (seconds)')
eps_pricing_last_seconds_nhh = Gauge('eps_pricing_last_seconds_nhh',
    'Time to price the most recently priced non-HH quote (seconds)')
eps_pricing_queue_depth = Gauge('eps_pricing_queue_depth',
    'Total pricing jobs across Horizon queues default/pricing-vcc/pricing-hh/pricing-nhh (from Redis)')
eps_pricing_last_sent_age_seconds = Gauge('eps_pricing_last_sent_age_seconds',
    'Seconds since the last quote was sent to price')
eps_pricing_last_received_age_seconds = Gauge('eps_pricing_last_received_age_seconds',
    'Seconds since the last price was received')
eps_pricing_oldest_in_queue_seconds = Gauge('eps_pricing_oldest_in_queue_seconds',
    'Age in seconds of the oldest pricing job across Horizon queues default/pricing-vcc/pricing-hh/pricing-nhh')
sites_priced_today = Gauge('sites_priced_today',
    'Number of sites priced today', ['fuel', 'type'])

# Econtracts
econtracts_sent_today = Gauge('econtracts_sent_today',
    'Contracts sent today', ['fuel'])
econtracts_signed_today = Gauge('econtracts_signed_today',
    'Contracts signed today', ['fuel'])

# Esignature
esignature_sent_today = Gauge('esignature_sent_today',
    'Signature requests sent today')
esignature_signed_today = Gauge('esignature_signed_today',
    'Signature requests signed today')
esignature_pending = Gauge('esignature_pending',
    'Signature requests awaiting signature', ['status'])

# SAN temperature state (from PowerStore alerts)
san_temperature_state = Gauge('san_temperature_state',
    'SAN node temperature state: 0=normal, 1=warning, 2=critical', ['node'])

# Server room environment (from Ubibot)
room_temperature = Gauge('room_temperature_celsius',
    'Server room temperature in Celsius', ['sensor'])
room_humidity = Gauge('room_humidity_percent',
    'Server room relative humidity percentage', ['sensor'])

# Server temperatures (from PRTG)
server_intake_temp = Gauge('server_intake_temp_celsius',
    'Server inlet/intake temperature in Celsius', ['server'])
server_exhaust_temp = Gauge('server_exhaust_temp_celsius',
    'Server exhaust temperature in Celsius', ['server'])
server_cpu1_temp = Gauge('server_cpu1_temp_celsius',
    'Server CPU1 temperature in Celsius', ['server'])
server_cpu2_temp = Gauge('server_cpu2_temp_celsius',
    'Server CPU2 temperature in Celsius', ['server'])


def get_genus_connection(fuel):
    """Create a pyodbc connection to Genus SQL Server."""
    cfg = GENUS_CONNECTIONS[fuel]
    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={cfg['host']};"
        f"DATABASE={cfg['database']};"
        f"UID={cfg['user']};"
        f"PWD={cfg['password']};"
        f"TrustServerCertificate=yes;"
        f"Connection Timeout=30;"
    )
    return pyodbc.connect(conn_str, timeout=30)


def get_mysql_connection(database):
    """Create a pymysql connection to a Crown MySQL database."""
    return pymysql.connect(
        host=MYSQL_CONFIG['host'],
        port=MYSQL_CONFIG['port'],
        user=MYSQL_CONFIG['user'],
        password=MYSQL_CONFIG['password'],
        database=database,
        connect_timeout=30,
    )


def get_pg_connection():
    """Create a psycopg2 connection to PostgreSQL."""
    return psycopg2.connect(**PG_CONFIG)


def query_single_value(cursor, sql, params=None):
    """Execute a query and return the first column of the first row, or 0."""
    try:
        cursor.execute(sql, params or [])
        row = cursor.fetchone()
        return row[0] if row and row[0] is not None else 0
    except Exception as e:
        logger.warning(f"Query failed: {e}")
        return 0


def init_pg_schema():
    """Create the genus_file_status table in PostgreSQL if it doesn't exist."""
    if not PG_CONFIG:
        return
    try:
        conn = get_pg_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS genus_file_status (
                app TEXT NOT NULL,
                filename TEXT NOT NULL,
                sent_at TIMESTAMP NOT NULL,
                processed BOOLEAN NOT NULL DEFAULT FALSE,
                processed_at TIMESTAMP,
                PRIMARY KEY (app, filename)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ewelcome_form_status (
                fuel TEXT NOT NULL,
                form_id BIGINT NOT NULL,
                slug TEXT,
                status TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                PRIMARY KEY (fuel, form_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS eps_failed_to_price (
                quote_id BIGINT PRIMARY KEY,
                quote_ref TEXT,
                sent_to_price_at TIMESTAMP,
                status TEXT
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("PostgreSQL genus_file_status table ready")
    except Exception as e:
        logger.error(f"Failed to init PG schema: {e}")


def collect_genus_metrics():
    """Check Genus connectivity, hub file freshness, and replica lag."""
    for fuel in GENUS_CONNECTIONS:
        start = time.time()
        try:
            conn = get_genus_connection(fuel)
            cursor = conn.cursor()

            genus_hub_file_latest_age_hours.labels(fuel=fuel).set(
                query_single_value(cursor,
                    "SELECT DATEDIFF(HOUR, MAX(DTProcessed), GETDATE()) FROM tblHubFileFlows"))

            genus_replica_lag_seconds.labels(fuel=fuel).set(
                query_single_value(cursor,
                    "SELECT DATEDIFF(SECOND, last_hardened_time, GETDATE()) "
                    "FROM sys.dm_hadr_database_replica_states "
                    "WHERE is_local = 1"))

            genus_up.labels(fuel=fuel).set(1)
            genus_query_duration.labels(fuel=fuel).set(time.time() - start)

            cursor.close()
            conn.close()
            logger.info(f"Genus {fuel} metrics collected in {time.time() - start:.2f}s")

        except Exception as e:
            genus_up.labels(fuel=fuel).set(0)
            genus_query_duration.labels(fuel=fuel).set(time.time() - start)
            logger.error(f"Genus {fuel} collection failed: {e}")


def collect_cross_reference():
    """Cross-reference files sent by EPS/GPS against Genus tblHubFileFlows.

    - Fetches recent filenames from MySQL genus_imports
    - Checks each against tblHubFileFlows (with processed timestamp)
    - Updates Prometheus metrics
    - Writes detailed file status to PostgreSQL for Grafana tables
    """
    if not MYSQL_CONFIG:
        return

    app_fuel = {'gps': 'gas', 'eps': 'elec'}

    for app_name, fuel in app_fuel.items():
        if fuel not in GENUS_CONNECTIONS:
            continue

        try:
            # Get recent filenames from MySQL (last 7 days)
            mysql_conn = get_mysql_connection(app_name)
            mysql_cur = mysql_conn.cursor()
            mysql_cur.execute(
                "SELECT filename, created_at FROM genus_imports "
                "WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY) "
                "AND deleted_at IS NULL "
                "ORDER BY created_at DESC")
            sent_files = mysql_cur.fetchall()
            mysql_cur.close()
            mysql_conn.close()

            if not sent_files:
                genus_imports_sent_today.labels(app=app_name).set(0)
                genus_imports_processed_today.labels(app=app_name).set(0)
                genus_imports_unprocessed.labels(app=app_name).set(0)
                genus_imports_oldest_unprocessed_minutes.labels(app=app_name).set(0)
                continue

            # Check which filenames exist in Genus and get their processed time
            genus_conn = get_genus_connection(fuel)
            genus_cur = genus_conn.cursor()

            filenames = [row[0] for row in sent_files]
            placeholders = ','.join(['?' for _ in filenames])
            genus_cur.execute(
                f"SELECT FileName, DTProcessed FROM tblHubFileFlows "
                f"WHERE FileName IN ({placeholders})",
                filenames)
            processed_map = {row[0]: row[1] for row in genus_cur.fetchall()}

            genus_cur.close()
            genus_conn.close()

            # Calculate metrics
            today = datetime.now().date()
            sent_today = [(fn, ts) for fn, ts in sent_files if ts.date() == today]
            processed_today = [fn for fn, ts in sent_today if fn in processed_map]
            unprocessed = [(fn, ts) for fn, ts in sent_files if fn not in processed_map]

            genus_imports_sent_today.labels(app=app_name).set(len(sent_today))
            genus_imports_processed_today.labels(app=app_name).set(len(processed_today))
            genus_imports_unprocessed.labels(app=app_name).set(len(unprocessed))

            if unprocessed:
                oldest_ts = min(ts for _, ts in unprocessed)
                age_minutes = (datetime.now() - oldest_ts).total_seconds() / 60
                genus_imports_oldest_unprocessed_minutes.labels(app=app_name).set(round(age_minutes))
            else:
                genus_imports_oldest_unprocessed_minutes.labels(app=app_name).set(0)

            # Write detailed status to PostgreSQL for Grafana table panels
            if PG_CONFIG:
                try:
                    pg_conn = get_pg_connection()
                    pg_cur = pg_conn.cursor()

                    # Upsert recent files (last 50 per app for the table view)
                    recent = sent_files[:50]
                    for fn, sent_at in recent:
                        is_processed = fn in processed_map
                        processed_at = processed_map.get(fn)
                        pg_cur.execute("""
                            INSERT INTO genus_file_status (app, filename, sent_at, processed, processed_at)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (app, filename) DO UPDATE SET
                                processed = EXCLUDED.processed,
                                processed_at = EXCLUDED.processed_at
                        """, (app_name, fn, sent_at, is_processed, processed_at))

                    pg_conn.commit()
                    pg_cur.close()
                    pg_conn.close()
                except Exception as e:
                    logger.error(f"{app_name} PG write failed: {e}")

            logger.info(f"{app_name}: {len(sent_today)} sent today, "
                        f"{len(processed_today)} processed, {len(unprocessed)} unprocessed (7d)")

        except Exception as e:
            logger.error(f"{app_name} cross-reference failed: {e}")


def collect_ewelcome_metrics():
    """Monitor ewelcome and ewelcome-power welcome pack forms."""
    if not MYSQL_CONFIG:
        return

    apps = {'gas': 'ewelcome', 'power': 'ewelcome_power'}

    for fuel, database in apps.items():
        try:
            conn = get_mysql_connection(database)
            cursor = conn.cursor()

            # Forms created today
            ewelcome_forms_created_today.labels(fuel=fuel).set(
                query_single_value(cursor,
                    "SELECT COUNT(*) FROM forms WHERE DATE(created_at) = CURDATE() AND deleted_at IS NULL"))

            # Forms completed today (status 3 = complete, updated today)
            ewelcome_forms_completed_today.labels(fuel=fuel).set(
                query_single_value(cursor,
                    "SELECT COUNT(*) FROM forms WHERE form_status_id = 3 "
                    "AND DATE(updated_at) = CURDATE() AND deleted_at IS NULL"))

            # Forms not started (status 1)
            ewelcome_forms_not_started.labels(fuel=fuel).set(
                query_single_value(cursor,
                    "SELECT COUNT(*) FROM forms WHERE form_status_id = 1 AND deleted_at IS NULL"))

            # Forms downloaded but not completed (status 2)
            ewelcome_forms_downloaded.labels(fuel=fuel).set(
                query_single_value(cursor,
                    "SELECT COUNT(*) FROM forms WHERE form_status_id = 2 AND deleted_at IS NULL"))

            # Failed jobs
            ewelcome_failed_jobs.labels(fuel=fuel).set(
                query_single_value(cursor,
                    "SELECT COUNT(*) FROM failed_jobs WHERE DATE(failed_at) = CURDATE()"))

            # Pending jobs
            ewelcome_pending_jobs.labels(fuel=fuel).set(
                query_single_value(cursor,
                    "SELECT COUNT(*) FROM jobs"))

            # Completion rate (all time)
            cursor.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN form_status_id = 3 THEN 1 ELSE 0 END) as completed "
                "FROM forms WHERE deleted_at IS NULL")
            row = cursor.fetchone()
            total, completed = row[0] or 0, row[1] or 0
            rate = round((completed / total) * 100, 1) if total > 0 else 0
            ewelcome_completion_rate.labels(fuel=fuel).set(rate)

            # Completion rate today
            cursor.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN form_status_id = 3 THEN 1 ELSE 0 END) as completed "
                "FROM forms WHERE DATE(created_at) = CURDATE() AND deleted_at IS NULL")
            row = cursor.fetchone()
            total_today, completed_today = row[0] or 0, row[1] or 0
            rate_today = round((completed_today / total_today) * 100, 1) if total_today > 0 else 0
            ewelcome_completion_rate_today.labels(fuel=fuel).set(rate_today)

            # Write recent forms to PG for table view
            if PG_CONFIG:
                try:
                    cursor.execute(
                        "SELECT f.id, f.slug, fs.display_name as status, f.created_at, f.updated_at "
                        "FROM forms f "
                        "JOIN form_statuses fs ON f.form_status_id = fs.id "
                        "WHERE f.deleted_at IS NULL "
                        "ORDER BY f.created_at DESC LIMIT 10")
                    recent_forms = cursor.fetchall()

                    pg_conn = get_pg_connection()
                    pg_cur = pg_conn.cursor()
                    for form_id, slug, status, created_at, updated_at in recent_forms:
                        pg_cur.execute("""
                            INSERT INTO ewelcome_form_status (fuel, form_id, slug, status, created_at, updated_at)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (fuel, form_id) DO UPDATE SET
                                status = EXCLUDED.status,
                                updated_at = EXCLUDED.updated_at
                        """, (fuel, form_id, slug, status, created_at, updated_at))
                    pg_conn.commit()
                    pg_cur.close()
                    pg_conn.close()
                except Exception as e:
                    logger.error(f"ewelcome {fuel} PG write failed: {e}")

            cursor.close()
            conn.close()
            logger.info(f"ewelcome {fuel}: {total} total forms, {completed} completed, rate {rate}%")

        except Exception as e:
            logger.error(f"ewelcome {fuel} collection failed: {e}")


def collect_eps_pricing_metrics():
    """Monitor EPS pricing queue: avg time to price (HH/NHH) and queue size."""
    if not MYSQL_CONFIG:
        return

    try:
        conn = get_mysql_connection('eps')
        cursor = conn.cursor()

        # Avg time to price today — HH quotes
        eps_pricing_avg_seconds_hh.set(
            query_single_value(cursor, """
                SELECT ROUND(AVG(t.time_to_price_seconds), 1)
                FROM (
                    SELECT q.id,
                        MAX(CASE WHEN s.half_hourly = 1 THEN 1 ELSE 0 END) AS has_hh,
                        TIMESTAMPDIFF(SECOND, q.sent_to_price_at, q.price_received_at) AS time_to_price_seconds
                    FROM quotes q
                    JOIN sites s ON s.quote_id = q.id
                    WHERE DATE(q.sent_to_price_at) = DATE(NOW())
                        AND q.price_received_at IS NOT NULL
                        AND q.sent_to_price_at <= q.price_received_at
                    GROUP BY q.id, q.sent_to_price_at, q.price_received_at
                ) t
                WHERE t.has_hh = 1
            """))

        # Avg time to price today — non-HH quotes
        eps_pricing_avg_seconds_nhh.set(
            query_single_value(cursor, """
                SELECT ROUND(AVG(t.time_to_price_seconds), 1)
                FROM (
                    SELECT q.id,
                        MAX(CASE WHEN s.half_hourly = 1 THEN 1 ELSE 0 END) AS has_hh,
                        TIMESTAMPDIFF(SECOND, q.sent_to_price_at, q.price_received_at) AS time_to_price_seconds
                    FROM quotes q
                    JOIN sites s ON s.quote_id = q.id
                    WHERE DATE(q.sent_to_price_at) = DATE(NOW())
                        AND q.price_received_at IS NOT NULL
                        AND q.sent_to_price_at <= q.price_received_at
                    GROUP BY q.id, q.sent_to_price_at, q.price_received_at
                ) t
                WHERE t.has_hh = 0
            """))

        # Last HH quote time to price (excluding manually approved curves)
        eps_pricing_last_seconds_hh.set(
            query_single_value(cursor, """
                SELECT ROUND(t.time_to_price_seconds, 1)
                FROM (
                    SELECT q.id,
                        MAX(CASE WHEN s.half_hourly = 1 THEN 1 ELSE 0 END) AS has_hh,
                        TIMESTAMPDIFF(SECOND, q.sent_to_price_at, q.price_received_at) AS time_to_price_seconds,
                        q.price_received_at
                    FROM quotes q
                    JOIN sites s ON s.quote_id = q.id
                    LEFT JOIN half_hourly_curve_status hhcs ON q.half_hourly_curve_status_id = hhcs.id
                    WHERE DATE(q.sent_to_price_at) = DATE(NOW())
                        AND q.price_received_at IS NOT NULL
                        AND q.sent_to_price_at <= q.price_received_at
                        AND (hhcs.name IS NULL OR hhcs.name != 'approved')
                    GROUP BY q.id, q.sent_to_price_at, q.price_received_at
                ) t
                WHERE t.has_hh = 1
                ORDER BY t.price_received_at DESC
                LIMIT 1
            """))

        # Last NHH quote time to price (excluding manually approved curves)
        eps_pricing_last_seconds_nhh.set(
            query_single_value(cursor, """
                SELECT ROUND(t.time_to_price_seconds, 1)
                FROM (
                    SELECT q.id,
                        MAX(CASE WHEN s.half_hourly = 1 THEN 1 ELSE 0 END) AS has_hh,
                        TIMESTAMPDIFF(SECOND, q.sent_to_price_at, q.price_received_at) AS time_to_price_seconds,
                        q.price_received_at
                    FROM quotes q
                    JOIN sites s ON s.quote_id = q.id
                    LEFT JOIN half_hourly_curve_status hhcs ON q.half_hourly_curve_status_id = hhcs.id
                    WHERE DATE(q.sent_to_price_at) = DATE(NOW())
                        AND q.price_received_at IS NOT NULL
                        AND q.sent_to_price_at <= q.price_received_at
                        AND (hhcs.name IS NULL OR hhcs.name != 'approved')
                    GROUP BY q.id, q.sent_to_price_at, q.price_received_at
                ) t
                WHERE t.has_hh = 0
                ORDER BY t.price_received_at DESC
                LIMIT 1
            """))

        # Use Python UK time for age calculations
        import zoneinfo
        uk_now = datetime.now(zoneinfo.ZoneInfo('Europe/London')).replace(tzinfo=None)

        # Total queue depth and oldest job — from Redis default queue
        if REDIS_CONFIG:
            try:
                import re as _re

                r = redis.Redis(
                    host=REDIS_CONFIG['host'],
                    port=REDIS_CONFIG['port'],
                    password=REDIS_CONFIG['password'],
                    decode_responses=True)
                prefix = REDIS_CONFIG['prefix']

                # Count pending jobs on the EPS pricing queues.
                # Pre-CT-1673 these jobs ran on `default`; post-CT-1673 they
                # are split across dedicated supervisors. Include both so the
                # metric is correct in either configuration.
                pricing_queues = {'default', 'pricing-vcc', 'pricing-hh', 'pricing-nhh', 'pricing-hh-small', 'pricing-hh-large', 'pricing-nhh-small', 'pricing-nhh-large'}
                pending_ids = r.zrange(f"{prefix}pending_jobs", 0, -1)
                one_hour_ago = uk_now.timestamp() - 3600
                all_default_jobs = []  # (site_id, created_ts)

                for job_id in pending_ids:
                    q = r.hget(f"{prefix}{job_id}", 'queue')
                    if q in pricing_queues:
                        created = r.hget(f"{prefix}{job_id}", 'created_at')
                        created_ts = float(created) if created else uk_now.timestamp()
                        site_id = None
                        payload = r.hget(f"{prefix}{job_id}", 'payload')
                        if payload:
                            try:
                                cmd = json.loads(payload).get('data', {}).get('command', '')
                                match = _re.search(r'"id";i:(\d+);', cmd)
                                if match:
                                    site_id = int(match.group(1))
                            except (json.JSONDecodeError, AttributeError):
                                pass
                        all_default_jobs.append((site_id, created_ts))

                # Query MySQL for all jobs: exclude stale/approved, get sent_to_price_at for age
                active_count = 0
                oldest_sent_at = None
                all_site_ids = [sid for sid, _ in all_default_jobs if sid]

                if all_site_ids and MYSQL_CONFIG:
                    try:
                        check_conn = get_mysql_connection('eps')
                        check_cursor = check_conn.cursor()
                        id_list = ','.join(str(sid) for sid in all_site_ids)
                        check_cursor.execute(
                            "SELECT s.id, s.pricing, q.sent_to_price_at, hhcs.name as curve_status "
                            "FROM sites s "
                            "JOIN quotes q ON s.quote_id = q.id "
                            "LEFT JOIN half_hourly_curve_status hhcs ON q.half_hourly_curve_status_id = hhcs.id "
                            f"WHERE s.id IN ({id_list})")
                        site_info = {}
                        for row in check_cursor.fetchall():
                            site_info[row[0]] = {
                                'pricing': row[1],
                                'sent_to_price_at': row[2],
                                'curve_status': row[3],
                            }
                        check_cursor.close()
                        check_conn.close()

                        for sid, _ in all_default_jobs:
                            info = site_info.get(sid)
                            if not info:
                                active_count += 1  # Unknown site, count it
                                continue
                            # Exclude stale (already priced) or manually approved
                            if info['pricing'] == 0 or info['curve_status'] == 'approved':
                                continue
                            active_count += 1
                            sent_at = info['sent_to_price_at']
                            if sent_at and (oldest_sent_at is None or sent_at < oldest_sent_at):
                                oldest_sent_at = sent_at
                    except Exception as e:
                        logger.warning(f"Queue MySQL check failed: {e}")
                        # Fallback: count all jobs, no exclusion
                        active_count = len(all_default_jobs)
                else:
                    active_count = len(all_default_jobs)

                eps_pricing_queue_depth.set(active_count)

                # Oldest job age from MySQL sent_to_price_at
                if oldest_sent_at:
                    eps_pricing_oldest_in_queue_seconds.set(
                        max(0, round((uk_now - oldest_sent_at).total_seconds())))
                else:
                    eps_pricing_oldest_in_queue_seconds.set(0)

            except Exception as e:
                logger.warning(f"Redis queue check failed: {e}")

        # Age of last sent/received
        cursor.execute("SELECT MAX(sent_to_price_at) FROM quotes WHERE sent_to_price_at IS NOT NULL")
        row = cursor.fetchone()
        if row and row[0]:
            eps_pricing_last_sent_age_seconds.set(max(0, round((uk_now - row[0]).total_seconds())))

        cursor.execute("SELECT MAX(price_received_at) FROM quotes WHERE price_received_at IS NOT NULL")
        row = cursor.fetchone()
        if row and row[0]:
            eps_pricing_last_received_age_seconds.set(max(0, round((uk_now - row[0]).total_seconds())))

        # Sites priced today (EPS/power) — split by HH/NHH
        sites_priced_today.labels(fuel='power', type='hh').set(
            query_single_value(cursor,
                "SELECT COUNT(DISTINCT s.id) FROM sites s "
                "JOIN quotes q ON s.quote_id = q.id "
                "WHERE DATE(q.price_received_at) = CURDATE() "
                "AND q.deleted_at IS NULL AND s.deleted_at IS NULL "
                "AND s.half_hourly = 1"))

        sites_priced_today.labels(fuel='power', type='nhh').set(
            query_single_value(cursor,
                "SELECT COUNT(DISTINCT s.id) FROM sites s "
                "JOIN quotes q ON s.quote_id = q.id "
                "WHERE DATE(q.price_received_at) = CURDATE() "
                "AND q.deleted_at IS NULL AND s.deleted_at IS NULL "
                "AND (s.half_hourly = 0 OR s.half_hourly IS NULL)"))

        cursor.close()
        conn.close()
        logger.info("EPS pricing metrics collected")

    except Exception as e:
        logger.error(f"EPS pricing collection failed: {e}")


def collect_gps_pricing_metrics():
    """Monitor GPS sites priced today."""
    if not MYSQL_CONFIG:
        return

    try:
        conn = get_mysql_connection('gps')
        cursor = conn.cursor()

        # GPS is all NHH
        sites_priced_today.labels(fuel='gas', type='nhh').set(
            query_single_value(cursor,
                "SELECT COUNT(DISTINCT s.id) FROM sites s "
                "JOIN quotes q ON s.quote_id = q.id "
                "WHERE DATE(q.last_quoted_date) = CURDATE() "
                "AND q.deleted_at IS NULL AND s.deleted_at IS NULL"))

        cursor.close()
        conn.close()
        logger.info("GPS pricing metrics collected")

    except Exception as e:
        logger.error(f"GPS pricing collection failed: {e}")


def collect_econtracts_metrics():
    """Monitor econtracts (gas & power) and esignature."""
    if not MYSQL_CONFIG:
        return

    # Econtracts gas & power
    for fuel, database in {'gas': 'econtracts', 'power': 'econtracts_power'}.items():
        try:
            conn = get_mysql_connection(database)
            cursor = conn.cursor()

            econtracts_sent_today.labels(fuel=fuel).set(
                query_single_value(cursor,
                    "SELECT COUNT(*) FROM offers WHERE DATE(created_at) = CURDATE()"))

            econtracts_signed_today.labels(fuel=fuel).set(
                query_single_value(cursor,
                    "SELECT COUNT(*) FROM offers WHERE DATE(contract_signed_at) = CURDATE()"))

            cursor.close()
            conn.close()
            logger.info(f"econtracts {fuel} metrics collected")

        except Exception as e:
            logger.error(f"econtracts {fuel} collection failed: {e}")

    # Esignature
    try:
        conn = get_mysql_connection('esignature')
        cursor = conn.cursor()

        esignature_sent_today.set(
            query_single_value(cursor,
                "SELECT COUNT(*) FROM signature_requests WHERE DATE(created_at) = CURDATE()"))

        esignature_signed_today.set(
            query_single_value(cursor,
                "SELECT COUNT(*) FROM signature_requests WHERE DATE(signed_at) = CURDATE()"))

        # Pending by status — today only
        cursor.execute(
            "SELECT status, COUNT(*) FROM signature_requests "
            "WHERE status IN ('sent', 'viewed', 'reassigned') "
            "AND DATE(created_at) = CURDATE() GROUP BY status")
        statuses = {row[0]: row[1] for row in cursor.fetchall()}
        for status in ('sent', 'viewed', 'reassigned'):
            esignature_pending.labels(status=status).set(statuses.get(status, 0))

        cursor.close()
        conn.close()
        logger.info("esignature metrics collected")

    except Exception as e:
        logger.error(f"esignature collection failed: {e}")


def collect_powerstore_metrics():
    """Monitor SAN temperature state from PowerStore active alerts."""
    if not POWERSTORE_CONFIG:
        return

    try:
        host = POWERSTORE_CONFIG['host']
        auth = (POWERSTORE_CONFIG['user'], POWERSTORE_CONFIG['password'])

        # Basic auth header
        import base64
        creds = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        url = (f"https://{host}/api/rest/alert?"
               "select=id,state,resource_name,events&state=eq.ACTIVE")
        req = urllib.request.Request(url, headers={
            'Authorization': f'Basic {creds}',
            'Content-Type': 'application/json',
        })
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            alerts = json.loads(resp.read())

        # Track worst temperature state per node
        node_states = {}

        for alert in alerts:
            for event in alert.get('events', []):
                name = event.get('name', '')
                resource = event.get('resource_name', '')
                if name == 'XMS_NODE_FP_TEMPERATURE_STATE_WARNING':
                    node_states[resource] = max(node_states.get(resource, 0), 1)
                elif name == 'XMS_NODE_FP_TEMPERATURE_STATE_HIGH':
                    node_states[resource] = max(node_states.get(resource, 0), 2)

        # Set gauges — default both nodes to 0 (normal)
        for node_name in ['NodeA', 'NodeB']:
            resource_name = f'BaseEnclosure-{node_name}'
            state = node_states.get(resource_name, 0)
            san_temperature_state.labels(node=node_name).set(state)

        logger.info(f"PowerStore metrics collected: {node_states if node_states else 'all normal'}")
    except Exception as e:
        logger.warning(f"PowerStore: {e}")


def collect_ubibot_metrics():
    """Collect server room temperature and humidity from each configured Ubibot sensor."""
    for sensor in UBIBOT_SENSORS:
        try:
            url = (f"https://webapi.ubibot.com/channels/{sensor['channel_id']}"
                   f"?api_key={sensor['api_key']}")
            req = urllib.request.Request(url, headers={'User-Agent': 'genus-monitor/1.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())

            last_values = json.loads(data['channel']['last_values'])

            temp = last_values.get('field1', {}).get('value')
            if temp is not None:
                room_temperature.labels(sensor=sensor['name']).set(round(temp, 1))

            humidity = last_values.get('field2', {}).get('value')
            if humidity is not None:
                room_humidity.labels(sensor=sensor['name']).set(round(humidity, 1))

            logger.info(f"Ubibot [{sensor['name']}]: temp={temp}, humidity={humidity}")
        except Exception as e:
            logger.warning(f"Ubibot [{sensor['name']}]: {e}")


def collect_prtg_metrics():
    """Collect server temperature data from PRTG."""
    if not PRTG_CONFIG:
        return

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    channel_map = {
        'Intake': server_intake_temp,
        'Exhaust': server_exhaust_temp,
        'CPU1 Temp': server_cpu1_temp,
        'CPU2 Temp': server_cpu2_temp,
    }

    for sensor_id, server_name in PRTG_TEMP_SENSORS.items():
        try:
            url = (f"{PRTG_CONFIG['url']}/api/table.json?content=channels"
                   f"&columns=name,lastvalue&id={sensor_id}"
                   f"&apitoken={PRTG_CONFIG['apitoken']}")
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                data = json.loads(resp.read())

            for ch in data.get('channels', []):
                gauge = channel_map.get(ch['name'])
                if gauge and ch.get('lastvalue_raw') is not None:
                    gauge.labels(server=server_name).set(ch['lastvalue_raw'])
        except Exception as e:
            logger.warning(f"PRTG sensor {sensor_id} ({server_name}): {e}")

    logger.info("PRTG temperature metrics collected")


def collect_metrics():
    """Run all metric collection."""
    collect_genus_metrics()
    collect_cross_reference()
    collect_ewelcome_metrics()
    collect_eps_pricing_metrics()
    collect_gps_pricing_metrics()
    collect_econtracts_metrics()
    collect_powerstore_metrics()
    collect_ubibot_metrics()
    collect_prtg_metrics()


def polling_loop():
    """Background thread that collects metrics on a schedule."""
    while True:
        collect_metrics()
        time.sleep(POLL_INTERVAL)


@app.route('/metrics')
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route('/temperature')
def temperature_widget():
    """Serve the temperature monitoring widget."""
    try:
        with open('/app/static/temperature-widget.html', 'r') as f:
            return Response(f.read(), mimetype='text/html')
    except FileNotFoundError:
        return Response('Widget not found', status=404)


@app.route('/health')
def health():
    return {
        'status': 'ok',
        'genus_connections': list(GENUS_CONNECTIONS.keys()),
        'mysql': MYSQL_CONFIG is not None,
        'postgres': PG_CONFIG is not None,
    }


if __name__ == '__main__':
    if not GENUS_CONNECTIONS:
        logger.error("No Genus connections configured. Set GENUS_GAS_* or GENUS_ELEC_* env vars.")
        exit(1)

    logger.info(f"Starting Genus monitor with connections: {list(GENUS_CONNECTIONS.keys())}")
    logger.info(f"MySQL monitoring: {'enabled' if MYSQL_CONFIG else 'disabled'}")
    logger.info(f"PostgreSQL: {'enabled' if PG_CONFIG else 'disabled'}")
    logger.info(f"Poll interval: {POLL_INTERVAL}s")

    init_pg_schema()

    # Initial collection
    collect_metrics()

    # Start background polling
    thread = threading.Thread(target=polling_loop, daemon=True)
    thread.start()

    app.run(host='0.0.0.0', port=3000)
