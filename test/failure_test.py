from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import json
import os
import ray
import sys
import tempfile
import threading
import time

import ray.ray_constants as ray_constants
import ray.test.cluster_utils
from ray.utils import _random_string
import pytest


def relevant_errors(error_type):
    return [info for info in ray.error_info() if info["type"] == error_type]


def wait_for_errors(error_type, num_errors, timeout=10):
    start_time = time.time()
    while time.time() - start_time < timeout:
        if len(relevant_errors(error_type)) >= num_errors:
            return
        time.sleep(0.1)
    raise Exception("Timing out of wait.")


@pytest.fixture
def ray_start_regular():
    # Start the Ray processes.
    ray.init(num_cpus=2)
    yield None
    # The code after the yield will run as teardown code.
    ray.shutdown()


@pytest.fixture
def shutdown_only():
    yield None
    # The code after the yield will run as teardown code.
    ray.shutdown()


def test_failed_task(ray_start_regular):
    @ray.remote
    def throw_exception_fct1():
        raise Exception("Test function 1 intentionally failed.")

    @ray.remote
    def throw_exception_fct2():
        raise Exception("Test function 2 intentionally failed.")

    @ray.remote(num_return_vals=3)
    def throw_exception_fct3(x):
        raise Exception("Test function 3 intentionally failed.")

    throw_exception_fct1.remote()
    throw_exception_fct1.remote()
    wait_for_errors(ray_constants.TASK_PUSH_ERROR, 2)
    assert len(relevant_errors(ray_constants.TASK_PUSH_ERROR)) == 2
    for task in relevant_errors(ray_constants.TASK_PUSH_ERROR):
        msg = task.get("message")
        assert "Test function 1 intentionally failed." in msg

    x = throw_exception_fct2.remote()
    try:
        ray.get(x)
    except Exception as e:
        assert "Test function 2 intentionally failed." in str(e)
    else:
        # ray.get should throw an exception.
        assert False

    x, y, z = throw_exception_fct3.remote(1.0)
    for ref in [x, y, z]:
        try:
            ray.get(ref)
        except Exception as e:
            assert "Test function 3 intentionally failed." in str(e)
        else:
            # ray.get should throw an exception.
            assert False

    @ray.remote
    def f():
        raise Exception("This function failed.")

    try:
        ray.get(f.remote())
    except Exception as e:
        assert "This function failed." in str(e)
    else:
        # ray.get should throw an exception.
        assert False


def test_fail_importing_remote_function(ray_start_regular):
    # Create the contents of a temporary Python file.
    temporary_python_file = """
def temporary_helper_function():
    return 1
"""

    f = tempfile.NamedTemporaryFile(suffix=".py")
    f.write(temporary_python_file.encode("ascii"))
    f.flush()
    directory = os.path.dirname(f.name)
    # Get the module name and strip ".py" from the end.
    module_name = os.path.basename(f.name)[:-3]
    sys.path.append(directory)
    module = __import__(module_name)

    # Define a function that closes over this temporary module. This should
    # fail when it is unpickled.
    @ray.remote
    def g():
        return module.temporary_python_file()

    wait_for_errors(ray_constants.REGISTER_REMOTE_FUNCTION_PUSH_ERROR, 2)
    errors = relevant_errors(ray_constants.REGISTER_REMOTE_FUNCTION_PUSH_ERROR)
    assert len(errors) == 2
    assert "No module named" in errors[0]["message"]
    assert "No module named" in errors[1]["message"]

    # Check that if we try to call the function it throws an exception and
    # does not hang.
    for _ in range(10):
        with pytest.raises(Exception):
            ray.get(g.remote())

    f.close()

    # Clean up the junk we added to sys.path.
    sys.path.pop(-1)


def test_failed_function_to_run(ray_start_regular):
    def f(worker):
        if ray.worker.global_worker.mode == ray.WORKER_MODE:
            raise Exception("Function to run failed.")

    ray.worker.global_worker.run_function_on_all_workers(f)
    wait_for_errors(ray_constants.FUNCTION_TO_RUN_PUSH_ERROR, 2)
    # Check that the error message is in the task info.
    errors = relevant_errors(ray_constants.FUNCTION_TO_RUN_PUSH_ERROR)
    assert len(errors) == 2
    assert "Function to run failed." in errors[0]["message"]
    assert "Function to run failed." in errors[1]["message"]


def test_fail_importing_actor(ray_start_regular):
    # Create the contents of a temporary Python file.
    temporary_python_file = """
def temporary_helper_function():
    return 1
"""

    f = tempfile.NamedTemporaryFile(suffix=".py")
    f.write(temporary_python_file.encode("ascii"))
    f.flush()
    directory = os.path.dirname(f.name)
    # Get the module name and strip ".py" from the end.
    module_name = os.path.basename(f.name)[:-3]
    sys.path.append(directory)
    module = __import__(module_name)

    # Define an actor that closes over this temporary module. This should
    # fail when it is unpickled.
    @ray.remote
    class Foo(object):
        def __init__(self):
            self.x = module.temporary_python_file()

        def get_val(self):
            return 1

    # There should be no errors yet.
    assert len(ray.error_info()) == 0

    # Create an actor.
    foo = Foo.remote()

    # Wait for the error to arrive.
    wait_for_errors(ray_constants.REGISTER_ACTOR_PUSH_ERROR, 1)
    errors = relevant_errors(ray_constants.REGISTER_ACTOR_PUSH_ERROR)
    assert "No module named" in errors[0]["message"]

    # Wait for the error from when the __init__ tries to run.
    wait_for_errors(ray_constants.TASK_PUSH_ERROR, 1)
    errors = relevant_errors(ray_constants.TASK_PUSH_ERROR)
    assert ("failed to be imported, and so cannot execute this method" in
            errors[0]["message"])

    # Check that if we try to get the function it throws an exception and
    # does not hang.
    with pytest.raises(Exception):
        ray.get(foo.get_val.remote())

    # Wait for the error from when the call to get_val.
    wait_for_errors(ray_constants.TASK_PUSH_ERROR, 2)
    errors = relevant_errors(ray_constants.TASK_PUSH_ERROR)
    assert ("failed to be imported, and so cannot execute this method" in
            errors[1]["message"])

    f.close()

    # Clean up the junk we added to sys.path.
    sys.path.pop(-1)


def test_failed_actor_init(ray_start_regular):
    error_message1 = "actor constructor failed"
    error_message2 = "actor method failed"

    @ray.remote
    class FailedActor(object):
        def __init__(self):
            raise Exception(error_message1)

        def fail_method(self):
            raise Exception(error_message2)

    a = FailedActor.remote()

    # Make sure that we get errors from a failed constructor.
    wait_for_errors(ray_constants.TASK_PUSH_ERROR, 1)
    errors = relevant_errors(ray_constants.TASK_PUSH_ERROR)
    assert len(errors) == 1
    assert error_message1 in errors[0]["message"]

    # Make sure that we get errors from a failed method.
    a.fail_method.remote()
    wait_for_errors(ray_constants.TASK_PUSH_ERROR, 2)
    errors = relevant_errors(ray_constants.TASK_PUSH_ERROR)
    assert len(errors) == 2
    assert error_message1 in errors[1]["message"]


def test_failed_actor_method(ray_start_regular):
    error_message2 = "actor method failed"

    @ray.remote
    class FailedActor(object):
        def __init__(self):
            pass

        def fail_method(self):
            raise Exception(error_message2)

    a = FailedActor.remote()

    # Make sure that we get errors from a failed method.
    a.fail_method.remote()
    wait_for_errors(ray_constants.TASK_PUSH_ERROR, 1)
    errors = relevant_errors(ray_constants.TASK_PUSH_ERROR)
    assert len(errors) == 1
    assert error_message2 in errors[0]["message"]


def test_incorrect_method_calls(ray_start_regular):
    @ray.remote
    class Actor(object):
        def __init__(self, missing_variable_name):
            pass

        def get_val(self, x):
            pass

    # Make sure that we get errors if we call the constructor incorrectly.

    # Create an actor with too few arguments.
    with pytest.raises(Exception):
        a = Actor.remote()

    # Create an actor with too many arguments.
    with pytest.raises(Exception):
        a = Actor.remote(1, 2)

    # Create an actor the correct number of arguments.
    a = Actor.remote(1)

    # Call a method with too few arguments.
    with pytest.raises(Exception):
        a.get_val.remote()

    # Call a method with too many arguments.
    with pytest.raises(Exception):
        a.get_val.remote(1, 2)
    # Call a method that doesn't exist.
    with pytest.raises(AttributeError):
        a.nonexistent_method()
    with pytest.raises(AttributeError):
        a.nonexistent_method.remote()


def test_worker_raising_exception(ray_start_regular):
    @ray.remote
    def f():
        ray.worker.global_worker._get_next_task_from_local_scheduler = None

    # Running this task should cause the worker to raise an exception after
    # the task has successfully completed.
    f.remote()

    wait_for_errors(ray_constants.WORKER_CRASH_PUSH_ERROR, 1)
    wait_for_errors(ray_constants.WORKER_DIED_PUSH_ERROR, 1)


def test_worker_dying(ray_start_regular):
    # Define a remote function that will kill the worker that runs it.
    @ray.remote
    def f():
        eval("exit()")

    f.remote()

    wait_for_errors(ray_constants.WORKER_DIED_PUSH_ERROR, 1)

    errors = relevant_errors(ray_constants.WORKER_DIED_PUSH_ERROR)
    assert len(errors) == 1
    assert "died or was killed while executing" in errors[0]["message"]


def test_actor_worker_dying(ray_start_regular):
    @ray.remote
    class Actor(object):
        def kill(self):
            eval("exit()")

    @ray.remote
    def consume(x):
        pass

    a = Actor.remote()
    [obj], _ = ray.wait([a.kill.remote()], timeout=5.0)
    with pytest.raises(Exception):
        ray.get(obj)
    with pytest.raises(Exception):
        ray.get(consume.remote(obj))
    wait_for_errors(ray_constants.WORKER_DIED_PUSH_ERROR, 1)


def test_actor_worker_dying_future_tasks(ray_start_regular):
    @ray.remote
    class Actor(object):
        def getpid(self):
            return os.getpid()

        def sleep(self):
            time.sleep(1)

    a = Actor.remote()
    pid = ray.get(a.getpid.remote())
    tasks1 = [a.sleep.remote() for _ in range(10)]
    os.kill(pid, 9)
    time.sleep(0.1)
    tasks2 = [a.sleep.remote() for _ in range(10)]
    for obj in tasks1 + tasks2:
        with pytest.raises(Exception):
            ray.get(obj)

    wait_for_errors(ray_constants.WORKER_DIED_PUSH_ERROR, 1)


def test_actor_worker_dying_nothing_in_progress(ray_start_regular):
    @ray.remote
    class Actor(object):
        def getpid(self):
            return os.getpid()

    a = Actor.remote()
    pid = ray.get(a.getpid.remote())
    os.kill(pid, 9)
    time.sleep(0.1)
    task2 = a.getpid.remote()
    with pytest.raises(Exception):
        ray.get(task2)


def test_actor_scope_or_intentionally_killed_message(ray_start_regular):
    @ray.remote
    class Actor(object):
        pass

    a = Actor.remote()
    a = Actor.remote()
    a.__ray_terminate__.remote()
    time.sleep(1)
    assert len(ray.error_info()) == 0, (
        "Should not have propogated an error - {}".format(ray.error_info()))


@pytest.fixture
def ray_start_object_store_memory():
    # Start the Ray processes.
    store_size = 10**6
    ray.init(num_cpus=1, object_store_memory=store_size)
    yield None
    # The code after the yield will run as teardown code.
    ray.shutdown()


@pytest.mark.skip("This test does not work yet.")
def test_put_error1(ray_start_object_store_memory):
    num_objects = 3
    object_size = 4 * 10**5

    # Define a task with a single dependency, a numpy array, that returns
    # another array.
    @ray.remote
    def single_dependency(i, arg):
        arg = np.copy(arg)
        arg[0] = i
        return arg

    @ray.remote
    def put_arg_task():
        # Launch num_objects instances of the remote task, each dependent
        # on the one before it. The result of the first task should get
        # evicted.
        args = []
        arg = single_dependency.remote(0, np.zeros(
            object_size, dtype=np.uint8))
        for i in range(num_objects):
            arg = single_dependency.remote(i, arg)
            args.append(arg)

        # Get the last value to force all tasks to finish.
        value = ray.get(args[-1])
        assert value[0] == i

        # Get the first value (which should have been evicted) to force
        # reconstruction. Currently, since we're not able to reconstruct
        # `ray.put` objects that were evicted and whose originating tasks
        # are still running, this for-loop should hang and push an error to
        # the driver.
        ray.get(args[0])

    put_arg_task.remote()

    # Make sure we receive the correct error message.
    wait_for_errors(ray_constants.PUT_RECONSTRUCTION_PUSH_ERROR, 1)


@pytest.mark.skip("This test does not work yet.")
def test_put_error2(ray_start_object_store_memory):
    # This is the same as the previous test, but it calls ray.put directly.
    num_objects = 3
    object_size = 4 * 10**5

    # Define a task with a single dependency, a numpy array, that returns
    # another array.
    @ray.remote
    def single_dependency(i, arg):
        arg = np.copy(arg)
        arg[0] = i
        return arg

    @ray.remote
    def put_task():
        # Launch num_objects instances of the remote task, each dependent
        # on the one before it. The result of the first task should get
        # evicted.
        args = []
        arg = ray.put(np.zeros(object_size, dtype=np.uint8))
        for i in range(num_objects):
            arg = single_dependency.remote(i, arg)
            args.append(arg)

        # Get the last value to force all tasks to finish.
        value = ray.get(args[-1])
        assert value[0] == i

        # Get the first value (which should have been evicted) to force
        # reconstruction. Currently, since we're not able to reconstruct
        # `ray.put` objects that were evicted and whose originating tasks
        # are still running, this for-loop should hang and push an error to
        # the driver.
        ray.get(args[0])

    put_task.remote()

    # Make sure we receive the correct error message.
    wait_for_errors(ray_constants.PUT_RECONSTRUCTION_PUSH_ERROR, 1)


def test_version_mismatch(shutdown_only):
    ray_version = ray.__version__
    ray.__version__ = "fake ray version"

    ray.init(num_cpus=1)

    wait_for_errors(ray_constants.VERSION_MISMATCH_PUSH_ERROR, 1)

    # Reset the version.
    ray.__version__ = ray_version


def test_warning_monitor_died(shutdown_only):
    ray.init(num_cpus=0)

    time.sleep(1)  # Make sure the monitor has started.

    # Cause the monitor to raise an exception by pushing a malformed message to
    # Redis. This will probably kill the raylets and the raylet_monitor in
    # addition to the monitor.
    fake_id = 20 * b"\x00"
    malformed_message = "asdf"
    redis_client = ray.worker.global_worker.redis_client
    redis_client.execute_command(
        "RAY.TABLE_ADD", ray.gcs_utils.TablePrefix.HEARTBEAT_BATCH,
        ray.gcs_utils.TablePubsub.HEARTBEAT_BATCH, fake_id, malformed_message)

    wait_for_errors(ray_constants.MONITOR_DIED_ERROR, 1)


def test_export_large_objects(ray_start_regular):
    import ray.ray_constants as ray_constants

    large_object = np.zeros(2 * ray_constants.PICKLE_OBJECT_WARNING_SIZE)

    @ray.remote
    def f():
        large_object

    # Make sure that a warning is generated.
    wait_for_errors(ray_constants.PICKLING_LARGE_OBJECT_PUSH_ERROR, 1)

    @ray.remote
    class Foo(object):
        def __init__(self):
            large_object

    Foo.remote()

    # Make sure that a warning is generated.
    wait_for_errors(ray_constants.PICKLING_LARGE_OBJECT_PUSH_ERROR, 2)


def test_warning_for_infeasible_tasks(ray_start_regular):
    # Check that we get warning messages for infeasible tasks.

    @ray.remote(num_gpus=1)
    def f():
        pass

    @ray.remote(resources={"Custom": 1})
    class Foo(object):
        pass

    # This task is infeasible.
    f.remote()
    wait_for_errors(ray_constants.INFEASIBLE_TASK_ERROR, 1)

    # This actor placement task is infeasible.
    Foo.remote()
    wait_for_errors(ray_constants.INFEASIBLE_TASK_ERROR, 2)


def test_warning_for_infeasible_zero_cpu_actor(shutdown_only):
    # Check that we cannot place an actor on a 0 CPU machine and that we get an
    # infeasibility warning (even though the actor creation task itself
    # requires no CPUs).

    ray.init(num_cpus=0)

    @ray.remote
    class Foo(object):
        pass

    # The actor creation should be infeasible.
    Foo.remote()
    wait_for_errors(ray_constants.INFEASIBLE_TASK_ERROR, 1)


def test_warning_for_too_many_actors(shutdown_only):
    # Check that if we run a workload which requires too many workers to be
    # started that we will receive a warning.
    num_cpus = 2
    ray.init(num_cpus=num_cpus)

    @ray.remote
    class Foo(object):
        def __init__(self):
            time.sleep(1000)

    [Foo.remote() for _ in range(num_cpus * 3)]
    wait_for_errors(ray_constants.WORKER_POOL_LARGE_ERROR, 1)
    [Foo.remote() for _ in range(num_cpus)]
    wait_for_errors(ray_constants.WORKER_POOL_LARGE_ERROR, 2)


def test_warning_for_too_many_nested_tasks(shutdown_only):
    # Check that if we run a workload which requires too many workers to be
    # started that we will receive a warning.
    num_cpus = 2
    ray.init(num_cpus=num_cpus)

    @ray.remote
    def f():
        time.sleep(1000)
        return 1

    @ray.remote
    def g():
        # Sleep so that the f tasks all get submitted to the scheduler after
        # the g tasks.
        time.sleep(1)
        ray.get(f.remote())

    [g.remote() for _ in range(num_cpus * 4)]
    wait_for_errors(ray_constants.WORKER_POOL_LARGE_ERROR, 1)


@pytest.fixture
def ray_start_two_nodes():
    # Start the Ray processes.
    cluster = ray.test.cluster_utils.Cluster()
    for _ in range(2):
        cluster.add_node(
            num_cpus=0,
            _internal_config=json.dumps({
                "num_heartbeats_timeout": 40
            }))
    ray.init(redis_address=cluster.redis_address)

    yield cluster
    # The code after the yield will run as teardown code.
    ray.shutdown()
    cluster.shutdown()


# Note that this test will take at least 10 seconds because it must wait for
# the monitor to detect enough missed heartbeats.
def test_warning_for_dead_node(ray_start_two_nodes):
    cluster = ray_start_two_nodes
    cluster.wait_for_nodes()

    client_ids = {item["ClientID"] for item in ray.global_state.client_table()}

    # Try to make sure that the monitor has received at least one heartbeat
    # from the node.
    time.sleep(0.5)

    # Kill both raylets.
    cluster.list_all_nodes()[1].kill_raylet()
    cluster.list_all_nodes()[0].kill_raylet()

    # Check that we get warning messages for both raylets.
    wait_for_errors(ray_constants.REMOVED_NODE_ERROR, 2, timeout=40)

    # Extract the client IDs from the error messages. This will need to be
    # changed if the error message changes.
    warning_client_ids = {
        item["message"].split(" ")[5]
        for item in relevant_errors(ray_constants.REMOVED_NODE_ERROR)
    }

    assert client_ids == warning_client_ids


def test_raylet_crash_when_get(ray_start_regular):
    nonexistent_id = ray.ObjectID(_random_string())

    def sleep_to_kill_raylet():
        # Don't kill raylet before default workers get connected.
        time.sleep(2)
        ray.services.all_processes[ray.services.PROCESS_TYPE_RAYLET][0].kill()

    thread = threading.Thread(target=sleep_to_kill_raylet)
    thread.start()
    with pytest.raises(Exception, match=r".*raylet client may be closed.*"):
        ray.get(nonexistent_id)
    thread.join()
