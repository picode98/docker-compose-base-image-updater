from typing import List, Dict, Tuple, AnyStr, Optional, NamedTuple
import re
import docker
import json
import yaml
import os.path
from pathlib import Path
from email.message import Message
from subprocess import check_call, CalledProcessError, TimeoutExpired

def parse_image_name(image_name: str):
    image_name_parts = image_name.split(':', maxsplit=2)
    return (image_name_parts[0], image_name_parts[1] if len(image_name_parts) >= 2 else 'latest')

def get_base_images(dockerfilePath: str) -> List[Tuple[str, str]]:
    regex: re.Pattern = re.compile('FROM\\s+(\\S+).*', flags=re.RegexFlag.IGNORECASE)
    result_list: List[str] = []
    with open(dockerfilePath, 'r') as dockerfile:
        lines: List[AnyStr] = dockerfile.readlines()
        for thisLine in lines:
            match: Optional[re.Match[AnyStr]] = regex.match(thisLine)

            if match != None:
                result_list.append(parse_image_name(match.group(1)))
    
    return result_list

def update_image(docker_client: docker.DockerClient, image_name: str, tag_name: str = 'latest') -> bool:
    existing_image_id = None
    try:
        existing_image_id = docker_client.images.get(f'{image_name}:{tag_name}').id
    except docker.errors.ImageNotFound:
        pass
    
    new_image = docker_client.images.pull(image_name, tag=tag_name)
    return existing_image_id != new_image.id

def get_docker_compose_dep_images(docker_compose_file_path: str) -> List[Tuple[str, str]]:
    result_list = []

    with open(docker_compose_file_path, 'r') as docker_compose_file:
        docker_compose_obj: Dict = yaml.safe_load(docker_compose_file)
        docker_compose_built_images = []

        svc_name: str
        svc_dict: Dict
        for (svc_name, svc_dict) in docker_compose_obj['services'].items():
            # print((svc_name, svc_dict))
            if 'build' in svc_dict.keys():
                docker_compose_built_images.append(parse_image_name(svc_dict['image'])[0])

                context_dir = svc_dict['build'].get('context')
                context_dir = context_dir if not(context_dir is None) else '.'

                dockerfile = Path(docker_compose_file_path).parent.joinpath(context_dir).joinpath(svc_dict['build']['dockerfile']).resolve()
                result_list += get_base_images(dockerfile)
            else:
                result_list.append(parse_image_name(svc_dict['image']))
    
    return [this_result for this_result in result_list if not(this_result[0] in docker_compose_built_images)]

class DockerComposeApp():
    def __init__(self, compose_file_path, images_to_pull=None, build_timeout=7200):
        self.compose_file_path = compose_file_path
        self.images_to_pull = images_to_pull if not(images_to_pull is None) else get_docker_compose_dep_images(compose_file_path)
        self.build_timeout = build_timeout

class PreviousRunData():
    def __init__(self):
        self.previous_image_builds_needed = set()

    def read(self, path: str):
        with open(path, 'r') as prev_run_file:
            prev_run_obj = json.load(prev_run_file)
            self.previous_image_builds_needed = set(prev_run_obj['builds_needed'])

    def write(self, path: str):
        with open(path, 'w') as prev_run_file:
            json.dump({'builds_needed': list(self.previous_image_builds_needed)}, prev_run_file)


def run_updates(docker_client: docker.DockerClient, docker_compose_apps: List[DockerComposeApp]):
    prev_run_path = 'previous_run.json'

    prev_run_data = PreviousRunData()
    try:
        prev_run_data.read(prev_run_path)
    except FileNotFoundError:
        pass
    
    for this_app in docker_compose_apps:
        any_image_updated = False
        for (this_image_name, this_image_tag) in this_app.images_to_pull:
            this_image_updated = update_image(docker_client, this_image_name, this_image_tag)
            any_image_updated = any_image_updated if any_image_updated else this_image_updated

            if this_image_updated:
                print(f'Updated {this_image_name}:{this_image_tag}.')
        
        if any_image_updated or (this_app.compose_file_path in prev_run_data.previous_image_builds_needed):
            print(f'Building and restarting image "{this_app.compose_file_path}"...')
            docker_compose_dir: Path = Path(this_app.compose_file_path).parent

            if not(this_app.compose_file_path in prev_run_data.previous_image_builds_needed):
                prev_run_data.previous_image_builds_needed.add(this_app.compose_file_path)
                prev_run_data.write(prev_run_path)

            build_success = True
            try:
                check_call(['docker-compose', 'build'], cwd=docker_compose_dir, timeout=this_app.build_timeout)
                check_call(['docker-compose', 'stop'], cwd=docker_compose_dir)
                check_call(['docker-compose', 'up', '--detach', '--force-recreate'], cwd=docker_compose_dir)
            except CalledProcessError:
                build_success = False
            except TimeoutExpired:
                build_success = False

            if build_success:
                print(f'Build for image "{this_app.compose_file_path}" succeeded.')
                prev_run_data.previous_image_builds_needed.remove(this_app.compose_file_path)
                prev_run_data.write(prev_run_path)
            else:
                print(f'Build for image "{this_app.compose_file_path}" FAILED.')
