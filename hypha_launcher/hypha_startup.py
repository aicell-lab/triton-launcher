import os
import json
import asyncio
import typing as T
from pathlib import Path

from executor.engine import Engine
from executor.engine.job.extend.webapp import WebappJob
from executor.engine.job.extend.subprocess import SubprocessJob
from executor.engine.job.base import Job
from executor.engine.job import ProcessJob
from executor.engine.utils import PortManager
from imjoy_rpc.hypha import connect_to_server
from pyotritonclient import get_config, execute

from .utils.log import get_logger
from .utils.misc import get_all_ips, check_ip_port
from .utils.hpc import SlurmSubprocess, detect_hpc_type
from .utils.container import ContainerEngine
from .constants import TRITON_IMAGE, IMJOY_APP_TEMPLATE, S3_IMAGE

logger = get_logger()


class LauncherTool:
    def __init__(
            self, server = None,
            store_dir: str = ".hypha_launcher_store",
            upstream_hypha_url: str = "https://ai.imjoy.io",
            upstream_service_id: str = "hypha-launcher",
            worker_type: T.Optional[str] = None,
            slurm_settings: T.Optional[T.Dict[str, str]] = None,
            debug: bool = False,
            ):
        self.server = server
        self.store_dir = Path(store_dir)
        assert upstream_hypha_url is not None, "Upstream hypha server is not provided."  # noqa
        assert upstream_service_id is not None, "Upstream service id is not provided."  # noqa
        self.upstream_hypha_url = upstream_hypha_url
        self.upstream_service_id = upstream_service_id
        self.debug = debug

        self.worker_type = worker_type

        self.slurm_settings = slurm_settings
        self.container_engine = ContainerEngine(self.store_dir / "containers")

    @property
    def server_port(self) -> int:
        public_url = self.server.config["public_base_url"]
        hypha_port = int(public_url.split(":")[-1])
        return hypha_port

    @property
    def hypha_url(self) -> str:
        ips = self._record_login_node_ips()
        login_node_ip = ips[0][1]  # use the first ip as the login node ip
        return f"http://{login_node_ip}:{self.server_port}"

    async def run(self):
        assert self.server is not None, "Server is not provided."
        self._record_hypha_server_port(self.server_port)
        if self.worker_type is not None:
            assert self.worker_type in [
                "slurm",
                "local",
            ], f"Invalid worker_type: {self.worker_type}"  # noqa
        else:
            self.worker_type = detect_hpc_type()

        logger.info(f"Computational environment(worker type): {self.worker_type}")
        if self.worker_type == "slurm":
            logger.info(f"Slurm settings: {self.slurm_settings}")
            if self.slurm_settings is None:
                logger.error("Slurm settings is not provided.")
                raise ValueError("Slurm settings is not provided.")
            assert (
                "account" in self.slurm_settings
            ), "account is required in slurm settings"  # noqa

        engine = Engine()
        worker_count = 0  # only increase
        workers_jobs: T.Dict[str, Job] = {}
        current_worker_id: T.Union[int, None] = None

        upstream_server = await connect_to_server({"server_url": self.upstream_hypha_url})  # noqa
        logger.info(
            f"Linking to upstream hypha server: {self.upstream_hypha_url}"
        )  # noqa

        async def launch_worker(sub_cmd: str, worker_type: T.Optional[str] = None) -> int:
            nonlocal worker_count
            worker_count += 1
            worker_id = f"worker_{worker_count}"
            cmd = f"python -m hypha_launcher.hypha_startup --store_dir={self.store_dir.as_posix()} - {sub_cmd} {worker_id}"  # noqa
            logger.info(f"Starting worker: {worker_id}")
            logger.info(f"Command: {cmd}")
            if worker_type is None:
                worker_type = self.worker_type
            if worker_type == "slurm":
                assert self.slurm_settings is not None
                cmd_job = SlurmSubprocess(cmd, **self.slurm_settings)
            else:
                cmd_job = SubprocessJob(cmd, base_class=ProcessJob)
            nonlocal current_worker_id
            current_worker_id = worker_id
            await engine.submit_async(cmd_job)
            workers_jobs[worker_id] = cmd_job
            return worker_id

        async def launch_triton_worker() -> int:
            return await launch_worker("run_triton_worker")

        async def launch_s3_server() -> int:
            return await launch_worker("run_s3_server", worker_type="local")

        async def launch_server_app(app_code: str):
            controller = await self.server.get_service("server-apps")
            imjoy_app_code = IMJOY_APP_TEMPLATE.format(app_code=app_code)
            config = await controller.launch(
                source=imjoy_app_code,
                config={"type": "web-python"},
            )
            assert "app_id" in config
            f = asyncio.Future()
            self.register_report_future(config["app_id"], f)
            return await f

        async def stop_worker(worker_id: str):
            if worker_id in workers_jobs:
                job = workers_jobs[worker_id]
                await job.cancel()
                del workers_jobs[worker_id]
                nonlocal current_worker_id
                if worker_id == current_worker_id:
                    if worker_count > 0:
                        new_worker_id = workers_jobs.keys()[0]
                        current_worker_id = new_worker_id
                    else:
                        current_worker_id = None
                return True
            return False

        up_service = {
            "name": "hypha-launcher",
            "id": self.upstream_service_id,
            "config": {"visibility": "public"},
            "launch_server_app": launch_server_app,
        }
        if self.debug:
            up_service["launch_worker"] = launch_worker
            up_service["stop_worker"] = stop_worker
        await upstream_server.register_service(up_service)
        #await launch_triton_worker()  # start a default worker
        await launch_s3_server()  # start a default worker

    async def run_s3_server(
            self,
            worker_id: str):
        """Run a worker server, run in the login node of HPC. """
        host_port_1: T.Union[int, None] = None
        host_port_2: T.Union[int, None] = None

        hypha_server_url = self._find_connectable_hypha_address()
        if hypha_server_url is None:
            raise ValueError("Cannot connect to hypha server.")

        async def start_worker_server(server_url: str):
            server = await connect_to_server({"server_url": server_url})
            await server.register_service(
                {
                    "name": "s3-worker",
                    "id": worker_id,
                    "config": {"visibility": "public"},
                }
            )

        engine = Engine()
        self.container_engine.pull_image(S3_IMAGE)

        data_dir = self.store_dir / "s3_data"
        data_dir.mkdir(exist_ok=True, parents=True)

        async def start_s3_server():
            def run_s3_server(host_port_1: int, host_port_2: int):
                self.container_engine.run_command(
                    f'server /data --console-address ":{host_port_2}" --address ":{host_port_1}"',  # noqa
                    S3_IMAGE,
                    ports={
                        host_port_1: 9000,
                        host_port_2: host_port_2,
                    },
                    volumes={str(data_dir): "/data"},
                )

            nonlocal host_port_1
            nonlocal host_port_2
            host_port_1 = PortManager.get_port()
            host_port_2 = PortManager.get_port()
            triton_job = ProcessJob(run_s3_server, args=(host_port_1, host_port_2))
            await engine.submit_async(triton_job)

        f1 = start_worker_server(hypha_server_url)
        f2 = start_s3_server()
        await asyncio.gather(f1, f2)

    async def run_triton_worker(
            self,
            worker_id: str,
            hypha_server_url: T.Optional[str] = None):
        """Run a worker server, run in the compute node of HPC. """
        host_triton_port: T.Union[int, None] = None

        if hypha_server_url is None:
            hypha_server_url = self._find_connectable_hypha_address()
            if hypha_server_url is None:
                raise ValueError("Cannot connect to hypha server.")

        async def start_worker_server(server_url: str):
            server = await connect_to_server({"server_url": server_url})

            async def get_triton_config(model_name: str, verbose: bool = False):  # noqa
                if host_triton_port is not None:
                    try:
                        res = await get_config(
                            f"http://127.0.0.1:{host_triton_port}",
                            model_name=model_name,
                            verbose=verbose,
                        )
                        return res
                    except Exception as e:
                        logger.error(f"Error: {e}")
                        return {"error": str(e)}
                else:
                    logger.error("Triton server is not started yet.")
                    return {"error": "Triton server is not started yet."}

            async def execute_triton(
                inputs: T.Union[T.Any, None] = None,
                model_name: T.Union[str, None] = None,
                cache_config: bool = True,
                **kwargs,
            ):
                if host_triton_port is not None:
                    try:
                        res = await execute(
                            inputs=inputs,
                            server_url=f"http://127.0.0.1:{host_triton_port}",
                            model_name=model_name,
                            cache_config=cache_config,
                            **kwargs,
                        )
                        return res
                    except Exception as e:
                        logger.error(f"Error: {e}")
                        return {"error": str(e)}
                else:
                    logger.error("Triton server is not started yet.")
                    return {"error": "Triton server is not started yet."}

            await server.register_service(
                {
                    "name": "triton-worker",
                    "id": worker_id,
                    "config": {"visibility": "public"},
                    "get_config": get_triton_config,
                    "execute": execute_triton,
                }
            )

        engine = Engine()
        self.container_engine.pull_image(TRITON_IMAGE)

        async def start_triton_server():
            def run_triton_server(host_port: int):
                self.container_engine.run_command(
                    f'bash -c "tritonserver --model-repository=/models --log-verbose=3 --log-info=1 --log-warning=1 --log-error=1 --model-control-mode=poll --exit-on-error=false --repository-poll-secs=10 --allow-grpc=False --http-port={host_port}"',  # noqa
                    TRITON_IMAGE,
                    ports={host_port: host_port},
                    volumes={str(self.store_dir / "models"): "/models"},
                )

            nonlocal host_triton_port
            host_triton_port = PortManager.get_port()
            triton_job = ProcessJob(run_triton_server, args=(host_triton_port,))
            await engine.submit_async(triton_job)

        f1 = start_worker_server(hypha_server_url)
        f2 = start_triton_server()
        await asyncio.gather(f1, f2)

    def _record_login_node_ips(self):
        ips = get_all_ips()
        logger.info("Possible login node IPs:", str(ips))
        ip_record_file = self.store_dir / "login_node_ips.txt"
        with open(ip_record_file, "w") as f:
            for ip in ips:
                f.write(f"{ip[0]}\t{ip[1]}\n")
        logger.info(f"Login node IPs are recorded to {ip_record_file}")
        return ips

    def _record_hypha_server_port(self, port: int):
        with open(self.store_dir / "hypha_server_port.txt", "w") as f:
            f.write(str(port))
        logger.info(f"Hypha server port is recorded to {port}")

    def _get_all_hypha_addresses(self) -> T.List[T.Tuple[str, str]]:
        with open(self.store_dir / "login_node_ips.txt") as f:
            lines = f.readlines()
        ips = [line.strip().split("\t") for line in lines]
        with open(self.store_dir / "hypha_server_port.txt") as f:
            port = int(f.read().strip())
        return [(ip[1], port) for ip in ips]

    def _find_connectable_hypha_address(self) -> T.Union[str, None]:
        hypha_server_url = None
        hypha_addresses = self._get_all_hypha_addresses()
        for ip, port in hypha_addresses:
            logger.info(f"Checking hypha server: {ip}:{port}")
            if check_ip_port(ip, port):
                hypha_server_url = f"http://{ip}:{port}"
                logger.info(f"Found connectable hypha server: {hypha_server_url}")  # noqa
                break
        return hypha_server_url


async def main(server):
    print("Hypha is starting up...")
    launcher_settings_str = os.environ.get("LAUNCHER_SETTINGS", None)
    if launcher_settings_str is None:
        raise ValueError("LAUNCHER_SETTINGS is not provided.")
    try:
        settings = json.loads(launcher_settings_str)
        logger.info(f"Launcher settings: {settings}")
        launcher = LauncherTool(
            server,
            store_dir=settings['store_dir'],
            upstream_hypha_url=settings['upstream_hypha_url'],
            upstream_service_id=settings['upstream_service_id'],
            worker_type=settings['worker_type'],
            slurm_settings=settings['slurm_settings'],
            debug=settings['debug'],
        )
        await launcher.run()
    except Exception as e:
        logger.error(f"Failed to start: {e}")


if __name__ == "__main__":
    import fire
    fire.Fire(LauncherTool)
