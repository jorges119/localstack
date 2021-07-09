import json
import logging
import os
import subprocess
from enum import Enum, unique
from typing import Dict, List, Optional, Tuple, Union

from localstack import config
from localstack.utils.bootstrap import PortMappings
from localstack.utils.common import TMP_FILES, rm_rf, safe_run, save_file, short_uid

LOG = logging.getLogger(__name__)


@unique
class DockerContainerStatus(Enum):
    DOWN = -1
    NOT_EXISTANT = 0
    UP = 1


class ContainerException(Exception):
    def __init__(self, message, stdout, stderr) -> None:
        self.message = message
        self.stdout = stdout
        self.stderr = stderr


class CmdDockerClient:
    """Class for managing docker containers using the command line executable"""

    def _docker_cmd(self) -> str:
        """Return the string to be used for running Docker commands."""
        return config.DOCKER_CMD

    def get_container_status(self, container_name: str) -> DockerContainerStatus:
        """Returns the status of the container with the given name"""
        cmd = [
            self._docker_cmd(),
            "ps",
            "-a",
            "--filter",
            f"name='{container_name}'",
            "--format",
            "{{ .Status }} - {{ .Names }}",
        ]
        LOG.debug('Getting status for container "%s": %s' % (container_name, cmd))
        cmd_result = safe_run(cmd)

        # filter empty / invalid lines from docker ps output
        cmd_result = next((line for line in cmd_result if container_name in line), '')
        container_status = cmd_result.strip().lower()
        if len(container_status) == 0:
            return DockerContainerStatus.NOT_EXISTANT
        elif container_status.startswith("up "):
            return DockerContainerStatus.UP
        else:
            return DockerContainerStatus.DOWN

    def get_network(self, container_name: str) -> str:
        """Returns the network mode of the container with the given name"""
        LOG.debug("Getting container network: %s" % container_name)
        cmd = [
            self._docker_cmd(),
            "inspect",
            container_name,
            "--format",
            "{{ .HostConfig.NetworkMode }}",
        ]

        LOG.debug(cmd)
        cmd_result = safe_run(cmd)

        container_network = cmd_result.strip()
        return container_network

    def stop_container(self, container_name: str) -> None:
        """Stops container with given name"""
        cmd = [self._docker_cmd(), "stop", "-t0", container_name]
        LOG.debug(cmd)
        safe_run(cmd)

    def remove_container(self, container_name: str) -> None:
        """Removes container with given name"""
        cmd = [self._docker_cmd(), "rm", "-f", container_name]
        safe_run(cmd, print_error=False)

    def list_containers(self, filter: Union[List[str], str, None] = None) -> List[dict]:
        """List all containers matching the given filters

        Returns a list of dicts with keys id, image, name, labels, status
        """
        filter = [filter] if isinstance(filter, str) else filter
        cmd = [self._docker_cmd(), "ps", "-a"]
        options = []
        if filter:
            options += [y for filter_item in filter for y in ["--filter", filter_item]]
        cmd += options
        cmd.append("--format")
        cmd.append(
            '{"id":"{{ .ID }}","image":"{{ .Image }}","name":"{{ .Names }}",'
            '"labels":"{{ .Labels }}","status":"{{ .State }}"}'
        )
        LOG.debug(cmd)
        cmd_result = safe_run(cmd).strip()
        container_list = []
        if cmd_result:
            container_list = list(map(lambda line: json.loads(line), cmd_result.split("\n")))
        return container_list

    def copy_into_container(self, container_name: str, local_path: str, container_path: str):
        cmd = [self._docker_cmd(), "cp", local_path, f"{container_name}:{container_path}"]
        LOG.debug(cmd)
        safe_run(cmd)

    def get_container_entrypoint(self, docker_image: str) -> str:
        """Get the entry point for the given image"""
        LOG.debug("Getting the entrypoint for image: %s" % (docker_image))
        cmd = [
            self._docker_cmd(),
            "image",
            "inspect",
            '--format="{{ .Config.Entrypoint }}"',
            docker_image,
        ]

        LOG.debug(cmd)
        run_result = safe_run(cmd)

        entry_point = run_result.strip("\"[]\n\r ")
        return entry_point

    def has_docker(self) -> bool:
        """Check if system has docker available"""
        try:
            safe_run([self._docker_cmd(), "ps"])
            return True
        except Exception:
            return False

    def create_container(self, image_name: str, **kwargs) -> str:
        cmd = self._build_run_create_cmd("create", image_name, **kwargs)
        try:
            container_id = safe_run(cmd)
            return container_id.strip()
        except subprocess.CalledProcessError as e:
            raise ContainerException(
                "Docker process returned with errorcode %s" % e.returncode, e.stdout, e.stderr
            )

    def run_container(
        self, image_name: str, asynchronous=False, stdin=None, **kwargs
    ) -> Union[Tuple[str, str], str]:
        cmd = self._build_run_create_cmd("run", image_name, **kwargs)
        kwargs = {}
        if asynchronous:
            kwargs = {
                "stdin": True,
                "inherit_env": True,
                "asynchronous": True,
                "stderr": subprocess.PIPE,
                "outfile": subprocess.PIPE,
            }
        try:
            process = safe_run(cmd, **kwargs)
            if asynchronous:
                stdout, stderr = process.communicate(input=stdin)
                if process.returncode != 0:
                    raise ContainerException(
                        "Docker process returned with error code %s" % process.returncode,
                        stdout,
                        stderr,
                    )
                else:
                    return (stdout, stderr)
        except subprocess.CalledProcessError as e:
            raise ContainerException(
                "Docker process returned with errorcode %s" % e.returncode, e.stdout, e.stderr
            )
        return process

    def exec_in_container(
        self,
        container_name_or_id: str,
        command: str,
        interactive=False,
        env_vars: Optional[List[Tuple[str, str]]] = None,
        asynchronous=False,
        stdin: Optional[str] = None,
    ) -> Union[Tuple[str, str], str]:
        cmd = [self._docker_cmd(), "exec"]
        if interactive:
            cmd.append("--interactive")
        if env_vars:
            cmd += Util.create_env_vars_file_flag(env_vars)
        cmd.append(container_name_or_id)
        cmd.append(command)
        kwargs = {}
        if asynchronous:
            kwargs = {
                "stdin": True,
                "inherit_env": True,
                "asynchronous": True,
                "stderr": subprocess.PIPE,
                "outfile": subprocess.PIPE,
            }
        try:
            process = safe_run(cmd, **kwargs)
            if asynchronous:
                stdout, stderr = process.communicate(input=stdin)
                if process.returncode != 0:
                    raise ContainerException(
                        "Docker process returned with error code %s" % process.returncode,
                        stdout,
                        stderr,
                    )
                else:
                    return (stdout, stderr)
        except subprocess.CalledProcessError as e:
            raise ContainerException(
                "Docker process returned with errorcode %s" % e.returncode, e.stdout, e.stderr
            )
        return process

    def start_container(
        self,
        container_name_or_id: str,
        asynchronous=False,
        stdin=None,
        interactive: bool = False,
        attach: bool = False,
        flags: Optional[str] = None,
    ):
        cmd = [self._docker_cmd()]
        if flags:
            cmd.append(flags)
        if interactive:
            cmd.append("--interactive")
        if attach:
            cmd.append("--attach")
        cmd.append('start')
        cmd.append(container_name_or_id)
        kwargs = {}
        if asynchronous:
            kwargs = {
                "stdin": True,
                "inherit_env": True,
                "asynchronous": True,
                "stderr": subprocess.PIPE,
                "outfile": subprocess.PIPE,
            }
        try:
            process = safe_run(cmd, **kwargs)
            if asynchronous:
                stdout, stderr = process.communicate(input=stdin)
                if process.returncode != 0:
                    raise ContainerException(
                        "Docker process returned with error code %s" % process.returncode,
                        stdout,
                        stderr,
                    )
                else:
                    return (stdout, stderr)
        except subprocess.CalledProcessError as e:
            raise ContainerException(
                "Docker process returned with errorcode %s" % e.returncode, e.stdout, e.stderr
            )
        return process

    def _build_run_create_cmd(
        self,
        action: str,
        image_name: str,
        *,
        name: Optional[str] = None,
        entrypoint: Optional[str] = None,
        remove: bool = False,
        interactive: bool = False,
        command: Optional[str] = None,
        mount_volumes: Optional[List[Tuple[str, str]]] = None,
        ports: Optional[PortMappings] = None,
        env_vars: Optional[Dict[str, str]] = None,
        network: Optional[str] = None,
        dns: Optional[str] = None,
        additional_flags: Optional[str] = None,
    ) -> List[str]:

        cmd = [self._docker_cmd(), action]
        if remove:
            cmd.append("--rm")
        if name:
            cmd += ["--name", name]
        if entrypoint:
            cmd += ["--entrypoint", entrypoint]
        if mount_volumes:
            cmd += [
                volume
                for host_path, docker_path in mount_volumes
                for volume in ["-v", f"{host_path}:{docker_path}"]
            ]
        if interactive:
            cmd.append("--interactive")
        if ports:
            cmd += ports.to_list()
        if env_vars:
            cmd += Util.create_env_vars_file_flag(env_vars)
        if network:
            cmd += ["--network", network]
        if dns:
            cmd += ["--dns", dns]
        if additional_flags:
            cmd += additional_flags.split()
        cmd.append(image_name)
        if command:
            cmd.append(command)
        return cmd


class Util:  # TODO remove duplicated code in lambda_executors
    MAX_ENV_ARGS_LENGTH = 20000

    @classmethod
    def create_env_vars_file_flag(cls, env_vars):
        if not env_vars:
            return []
        result = []
        env_vars = dict(env_vars)
        if len(str(env_vars)) > cls.MAX_ENV_ARGS_LENGTH:
            # default ARG_MAX=131072 in Docker - let's create an env var file if the string becomes too long...
            env_file = cls.mountable_tmp_file()
            env_content = ""
            for name, value in dict(env_vars).items():
                if len(value) > cls.MAX_ENV_ARGS_LENGTH:
                    # each line in the env file has a max size as well (error "bufio.Scanner: token too long")
                    continue
                env_vars.pop(name)
                value = value.replace("\n", "\\")
                env_content += "%s=%s\n" % (name, value)
            save_file(env_file, env_content)
            result += ["--env-file", env_file]

        env_vars_res = [item for k, v in env_vars.items() for item in ["-e", "{}={}".format(k, v)]]
        result += env_vars_res
        return result

    @staticmethod
    def rm_env_vars_file(env_vars_file_flag):
        if not env_vars_file_flag or "--env-file" not in env_vars_file_flag:
            return
        env_vars_file = env_vars_file_flag.replace("--env-file", "").strip()
        return rm_rf(env_vars_file)

    @staticmethod
    def mountable_tmp_file():
        f = os.path.join(config.TMP_FOLDER, short_uid())
        TMP_FILES.append(f)
        return f


DOCKER_CLIENT = CmdDockerClient()
