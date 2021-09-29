import os
from contextlib import closing
from fixtures.zenith_fixtures import PostgresFactory, ZenithPageserver

import logging
import fixtures.log_helper  # configures loggers
log = logging.getLogger('root')

pytest_plugins = ("fixtures.zenith_fixtures", "fixtures.benchmark_fixture")

def get_timeline_size(repo_dir: str, tenantid: str, timelineid: str):
    path = "{}/tenants/{}/timelines/{}".format(repo_dir, tenantid, timelineid)

    totalbytes = 0
    for root, dirs, files in os.walk(path):
        for name in files:
            totalbytes += os.path.getsize(os.path.join(root, name))

        if 'wal' in dirs:
            dirs.remove('wal')  # don't visit 'wal' subdirectory

    return totalbytes

#
# Run bulk INSERT test.
#
# Collects metrics:
#
# 1. Time to INSERT 5 million rows
# 2. Disk writes
# 3. Disk space used
#
def test_bulk_insert(postgres: PostgresFactory, pageserver: ZenithPageserver, pg_bin, zenith_cli, zenbenchmark, repo_dir: str):
    # Create a branch for us
    zenith_cli.run(["branch", "test_bulk_insert", "empty"])

    pg = postgres.create_start('test_bulk_insert')
    log.info("postgres is running on 'test_bulk_insert' branch")

    # Open a connection directly to the page server that we'll use to force
    # flushing the layers to disk
    psconn = pageserver.connect();
    pscur = psconn.cursor()

    # Get the timeline ID of our branch. We need it for the 'do_gc' command
    with closing(pg.connect()) as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW zenith.zenith_timeline")
            timeline = cur.fetchone()[0]

            cur.execute("create table huge (i int, j int);")

            # Run INSERT, recording the time and I/O it takes
            with zenbenchmark.record_pageserver_writes(pageserver, 'pageserver_writes'):
                with zenbenchmark.record_duration('insert'):
                    cur.execute("insert into huge values (generate_series(1, 5000000), 0);")

                    # Flush the layers from memory to disk. This is included in the reported
                    # time and I/O
                    pscur.execute(f"do_gc {pageserver.initial_tenant} {timeline} 0")

            # Report disk space used by the repository
            timeline_size = get_timeline_size(repo_dir, pageserver.initial_tenant, timeline)
            zenbenchmark.record('size', timeline_size / (1024*1024), 'MB')
