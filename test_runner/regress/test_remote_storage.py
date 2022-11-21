# It's possible to run any regular test with the local fs remote storage via
# env NEON_PAGESERVER_OVERRIDES="remote_storage={local_path='/tmp/neon_zzz/'}" poetry ......

import os
import shutil
import time
from pathlib import Path
import re

import pytest
from fixtures.log_helper import log
from fixtures.neon_fixtures import (
    NeonEnvBuilder,
    RemoteStorageKind,
    available_remote_storages,
    wait_for_last_record_lsn,
    wait_for_last_flush_lsn,
    wait_for_upload,
)
from fixtures.types import Lsn, TenantId, TimelineId
from fixtures.utils import query_scalar, wait_until, print_gc_result


#
# Tests that a piece of data is backed up and restored correctly:
#
# 1. Initial pageserver
#   * starts a pageserver with remote storage, stores specific data in its tables
#   * triggers a checkpoint (which produces a local data scheduled for backup), gets the corresponding timeline id
#   * polls the timeline status to ensure it's copied remotely
#   * inserts more data in the pageserver and repeats the process, to check multiple checkpoints case
#   * stops the pageserver, clears all local directories
#
# 2. Second pageserver
#   * starts another pageserver, connected to the same remote storage
#   * timeline_attach is called for the same timeline id
#   * timeline status is polled until it's downloaded
#   * queries the specific data, ensuring that it matches the one stored before
#
# The tests are done for all types of remote storage pageserver supports.
@pytest.mark.parametrize("remote_storage_kind", available_remote_storages())
def test_remote_storage_backup_and_restore(
    neon_env_builder: NeonEnvBuilder,
    remote_storage_kind: RemoteStorageKind,
):
    # Use this test to check more realistic SK ids: some etcd key parsing bugs were related,
    # and this test needs SK to write data to pageserver, so it will be visible
    neon_env_builder.safekeepers_id_start = 12

    neon_env_builder.enable_remote_storage(
        remote_storage_kind=remote_storage_kind,
        test_name="test_remote_storage_backup_and_restore",
    )

    data_id = 1
    data_secret = "very secret secret"

    ##### First start, insert secret data and upload it to the remote storage
    env = neon_env_builder.init_start()

    # FIXME: Is this expected?
    env.pageserver.allowed_errors.append(
        ".*marking .* as locally complete, while it doesnt exist in remote index.*"
    )
    env.pageserver.allowed_errors.append(".*No timelines to attach received.*")

    env.pageserver.allowed_errors.append(".*Failed to get local tenant state.*")
    # FIXME retry downloads without throwing errors
    env.pageserver.allowed_errors.append(".*failed to load remote timeline.*")
    # we have a bunch of pytest.raises for these below
    env.pageserver.allowed_errors.append(".*tenant already exists.*")
    env.pageserver.allowed_errors.append(".*attach is already in progress.*")

    pageserver_http = env.pageserver.http_client()
    pg = env.postgres.create_start("main")

    client = env.pageserver.http_client()

    tenant_id = TenantId(pg.safe_psql("show neon.tenant_id")[0][0])
    timeline_id = TimelineId(pg.safe_psql("show neon.timeline_id")[0][0])

    checkpoint_numbers = range(1, 3)

    for checkpoint_number in checkpoint_numbers:
        with pg.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE t{checkpoint_number}(id int primary key, secret text);
                INSERT INTO t{checkpoint_number} VALUES ({data_id}, '{data_secret}|{checkpoint_number}');
            """
            )
            current_lsn = Lsn(query_scalar(cur, "SELECT pg_current_wal_flush_lsn()"))

        # wait until pageserver receives that data
        wait_for_last_record_lsn(client, tenant_id, timeline_id, current_lsn)

        # run checkpoint manually to be sure that data landed in remote storage
        pageserver_http.timeline_checkpoint(tenant_id, timeline_id)

        log.info(f"waiting for checkpoint {checkpoint_number} upload")

        # insert upload failpoints to exercise retry code path
        action = "3*return->off" if checkpoint_number == 0 else "off"
        pageserver_http.configure_failpoints(
            [
                ("before-upload-layer", action),
                ("before-upload-index", action),
            ]
        )
        # wait until pageserver successfully uploaded a checkpoint to remote storage
        wait_for_upload(client, tenant_id, timeline_id, current_lsn)
        log.info(f"upload of checkpoint {checkpoint_number} is done")

    ##### Stop the first pageserver instance, erase all its data
    env.postgres.stop_all()
    env.pageserver.stop()

    dir_to_clear = Path(env.repo_dir) / "tenants"
    shutil.rmtree(dir_to_clear)
    os.mkdir(dir_to_clear)

    ##### Second start, restore the data and ensure it's the same
    env.pageserver.start()

    # Introduce failpoint in download
    pageserver_http.configure_failpoints(("remote-storage-download-pre-rename", "return"))

    client.tenant_attach(tenant_id)

    # is there a better way to assert that failpoint triggered?
    time.sleep(10)

    # assert cannot attach timeline that is scheduled for download
    # FIXME implement layer download retries
    with pytest.raises(Exception, match="tenant already exists, current state: Broken"):
        client.tenant_attach(tenant_id)

    tenant_status = client.tenant_status(tenant_id)
    log.info("Tenant status with active failpoint: %s", tenant_status)
    # FIXME implement layer download retries
    # assert tenant_status["has_in_progress_downloads"] is True

    # Ensure that an initiated attach operation survives pageserver restart.
    # It will start downloading as soon as the tenant state has been loaded from disk.
    # Eventually it will transition from Attaching => Active.
    env.pageserver.stop()
    env.pageserver.start()
    log.info("waiting for timeline to finish attaching")

    def assert_attach_fails_and_check_if_reached_active():
        with pytest.raises(Exception, match="tenant already exists.*(Attaching|Active)"):
            client.tenant_attach(tenant_id)
        all_states = client.tenant_list()
        [tenant] = [t for t in all_states if TenantId(t["id"]) == tenant_id]
        assert tenant["has_in_progress_downloads"] == False
        assert tenant["state"] == {"Active": {"background_jobs_running": True}}

    wait_until(
        number_of_iterations=20,
        interval=1,
        func=assert_attach_fails_and_check_if_reached_active,
    )

    detail = client.timeline_detail(tenant_id, timeline_id)
    log.info("Timeline detail after attach completed: %s", detail)
    assert (
        Lsn(detail["last_record_lsn"]) >= current_lsn
    ), "current db Lsn should should not be less than the one stored on remote storage"
    assert not detail["awaits_download"]

    pg = env.postgres.create_start("main")
    with pg.cursor() as cur:
        for checkpoint_number in checkpoint_numbers:
            assert (
                query_scalar(cur, f"SELECT secret FROM t{checkpoint_number} WHERE id = {data_id};")
                == f"{data_secret}|{checkpoint_number}"
            )


@pytest.mark.parametrize("remote_storage_kind", [RemoteStorageKind.LOCAL_FS])
def test_remote_storage_upload_queue_retries(
    neon_env_builder: NeonEnvBuilder,
    remote_storage_kind: RemoteStorageKind,
):

    neon_env_builder.enable_remote_storage(
        remote_storage_kind=remote_storage_kind,
        test_name="test_remote_storage_backup_and_restore",
    )

    env = neon_env_builder.init_start()

    # create tenant with config that will determinstically allow
    # compaction and gc
    tenant_id, timeline_id = env.neon_cli.create_tenant(
        conf={
            # "gc_horizon": f"{1024 ** 2}",
            "checkpoint_distance": f"{1024 ** 2}",
            "compaction_threshold": "1",
            "compaction_target_size": f"{1024 ** 2}",
            # small PITR interval to allow gc
            "pitr_interval": "1 s",
        }
    )

    client = env.pageserver.http_client()

    pg = env.postgres.create_start("main", tenant_id=tenant_id)

    pg.safe_psql("CREATE TABLE foo (id INTEGER PRIMARY KEY, val text)")

    def configure_storage_sync_failpoints(action):
        client.configure_failpoints(
            [
                ("before-upload-layer", action),
                ("before-upload-index", action),
                ("before-delete-layer", action),
            ]
        )

    def overwrite_data_and_wait_for_it_to_arrive_at_pageserver():
        # create initial set of layers & upload them with failpoints configured
        pg.safe_psql(
            """
               INSERT INTO foo (id, val)
               SELECT g, CURRENT_TIMESTAMP
               FROM generate_series(1, 100000) g
               ON CONFLICT (id) DO UPDATE
               SET val = EXCLUDED.val
               """
        )
        wait_for_last_flush_lsn(env, pg, tenant_id, timeline_id)

    # let all of them queue up
    configure_storage_sync_failpoints("return")

    overwrite_data_and_wait_for_it_to_arrive_at_pageserver()
    client.timeline_checkpoint(tenant_id, timeline_id)
    # now overwrite it again
    overwrite_data_and_wait_for_it_to_arrive_at_pageserver()
    # trigger layer deletion by doing Compaction, then GC
    client.timeline_compact(tenant_id, timeline_id)
    gc_result = client.timeline_gc(tenant_id, timeline_id, 0)
    print_gc_result(gc_result)
    assert gc_result["layers_removed"] > 0

    # confirm all operations are queued up
    def get_queued_count():
        metrics = client.get_metrics()
        matches = re.search(
            f'^pageserver_remote_upload_queue_items{{tenant_id="{tenant_id}",timeline_id="{timeline_id}"}} (\\S+)$',
            metrics,
            re.MULTILINE,
        )
        assert matches

    assert get_queued_count() > 0

    configure_storage_sync_failpoints("off")

    wait_until(10, 1, lambda: get_queued_count() == 0)
