import json
import random

from main import extract_content_from_result, write_config_yml, get_all_executor_files_with_content, files_to_string
from src import gpt, jina_cloud
from src.constants import FILE_AND_TAG_PAIRS
from src.jina_cloud import push_executor, process_error_message
from src.prompt_tasks import general_guidelines, executor_file_task, chain_of_thought_creation, test_executor_file_task, \
    chain_of_thought_optimization, requirements_file_task, docker_file_task, not_allowed
from src.utils.io import recreate_folder, persist_file
from src.utils.string_tools import print_colored


def wrap_content_in_code_block(executor_content, file_name, tag):
    return f'**{file_name}**\n```{tag}\n{executor_content}\n```\n\n'


def create_executor(
        executor_description,
        test_scenario,
        executor_name,
        package,
        is_chain_of_thought=False,
):
    EXECUTOR_FOLDER_v1 = get_executor_path(package, 1)
    recreate_folder(EXECUTOR_FOLDER_v1)
    recreate_folder('flow')

    print_colored('', '############# Executor #############', 'red')
    user_query = (
            general_guidelines()
            + executor_file_task(executor_name, executor_description, test_scenario, package)
            + chain_of_thought_creation()
    )
    conversation = gpt.Conversation()
    executor_content_raw = conversation.query(user_query)
    if is_chain_of_thought:
        executor_content_raw = conversation.query(
            f"General rules: " + not_allowed() + chain_of_thought_optimization('python', 'executor.py'))
    executor_content = extract_content_from_result(executor_content_raw, 'executor.py')

    persist_file(executor_content, EXECUTOR_FOLDER_v1 + '/executor.py')

    print_colored('', '############# Test Executor #############', 'red')
    user_query = (
            general_guidelines()
            + wrap_content_in_code_block(executor_content, 'executor.py', 'python')
            + test_executor_file_task(executor_name, test_scenario)
    )
    conversation = gpt.Conversation()
    test_executor_content_raw = conversation.query(user_query)
    if is_chain_of_thought:
        test_executor_content_raw = conversation.query(
            f"General rules: " + not_allowed() +
            chain_of_thought_optimization('python', 'test_executor.py')
            + "Don't add any additional tests. "
        )
    test_executor_content = extract_content_from_result(test_executor_content_raw, 'test_executor.py')
    persist_file(test_executor_content, EXECUTOR_FOLDER_v1 + '/test_executor.py')

    print_colored('', '############# Requirements #############', 'red')
    user_query = (
            general_guidelines()
            + wrap_content_in_code_block(executor_content, 'executor.py', 'python')
            + wrap_content_in_code_block(test_executor_content, 'test_executor.py', 'python')
            + requirements_file_task()
    )
    conversation = gpt.Conversation()
    requirements_content_raw = conversation.query(user_query)
    if is_chain_of_thought:
        requirements_content_raw = conversation.query(
            chain_of_thought_optimization('', 'requirements.txt') + "Keep the same version of jina ")

    requirements_content = extract_content_from_result(requirements_content_raw, 'requirements.txt')
    persist_file(requirements_content, EXECUTOR_FOLDER_v1 + '/requirements.txt')

    print_colored('', '############# Dockerfile #############', 'red')
    user_query = (
            general_guidelines()
            + wrap_content_in_code_block(executor_content, 'executor.py', 'python')
            + wrap_content_in_code_block(test_executor_content, 'test_executor.py', 'python')
            + wrap_content_in_code_block(requirements_content, 'requirements.txt', '')
            + docker_file_task()
    )
    conversation = gpt.Conversation()
    dockerfile_content_raw = conversation.query(user_query)
    if is_chain_of_thought:
        dockerfile_content_raw = conversation.query(
            f"General rules: " + not_allowed() + chain_of_thought_optimization('dockerfile', 'Dockerfile'))
    dockerfile_content = extract_content_from_result(dockerfile_content_raw, 'Dockerfile')
    persist_file(dockerfile_content, EXECUTOR_FOLDER_v1 + '/Dockerfile')

    write_config_yml(executor_name, EXECUTOR_FOLDER_v1)


def create_playground(executor_name, executor_path, host):
    print_colored('', '############# Playground #############', 'red')

    file_name_to_content = get_all_executor_files_with_content(executor_path)
    user_query = (
            general_guidelines()
            + wrap_content_in_code_block(file_name_to_content['executor.py'], 'executor.py', 'python')
            + wrap_content_in_code_block(file_name_to_content['test_executor.py'], 'test_executor.py', 'python')
            + f'''
Create a playground for the executor {executor_name} using streamlit. 
The executor is hosted on {host}. 
This is an example how you can connect to the executor assuming the document (d) is already defined:
from jina import Client, Document, DocumentArray
client = Client(host='{host}')
response = client.post('/process', inputs=DocumentArray([d]))
print(response[0].text) # can also be blob in case of image/audio..., this should be visualized in the streamlit app
'''
    )
    conversation = gpt.Conversation()
    conversation.query(user_query)
    playground_content_raw = conversation.query(
        f"General rules: " + not_allowed() + chain_of_thought_optimization('python', 'app.py'))
    playground_content = extract_content_from_result(playground_content_raw, 'app.py')
    persist_file(playground_content, f'{executor_path}/app.py')

def get_executor_path(package, version):
    package_path = '_'.join(package)
    return f'executor/{package_path}/v{version}'

def debug_executor(package, executor_description, test_scenario):
    MAX_DEBUGGING_ITERATIONS = 10
    error_before = ''
    for i in range(1, MAX_DEBUGGING_ITERATIONS):
        previous_executor_path = get_executor_path(package, i)
        next_executor_path = get_executor_path(package, i + 1)
        log_hubble = push_executor(previous_executor_path)
        error = process_error_message(log_hubble)
        if error:
            recreate_folder(next_executor_path)
            file_name_to_content = get_all_executor_files_with_content(previous_executor_path)
            all_files_string = files_to_string(file_name_to_content)
            user_query = (
                    f"General rules: " + not_allowed()
                    + 'Here is the description of the task the executor must solve:\n'
                    + executor_description
                    + '\n\nHere is the test scenario the executor must pass:\n'
                    + test_scenario
                    + 'Here are all the files I use:\n'
                    + all_files_string
                    + (('This is an error that is already fixed before:\n'
                        + error_before) if error_before else '')
                    + '\n\nNow, I get the following error:\n'
                    + error + '\n'
                    + 'Think quickly about possible reasons. '
                      'Then output the files that need change. '
                      "Don't output files that don't need change. "
                      "If you output a file, then write the complete file. "
                      "Use the exact same syntax to wrap the code:\n"
                      f"**...**\n"
                      f"```...\n"
                      f"...code...\n"
                      f"```\n\n"
            )
            conversation = gpt.Conversation()
            returned_files_raw = conversation.query(user_query)
            for file_name, tag in FILE_AND_TAG_PAIRS:
                updated_file = extract_content_from_result(returned_files_raw, file_name)
                if updated_file:
                    file_name_to_content[file_name] = updated_file

            for file_name, content in file_name_to_content.items():
                persist_file(content, f'{next_executor_path}/{file_name}')
            error_before = error

        else:
            break
        if i == MAX_DEBUGGING_ITERATIONS - 1:
            raise Exception('Could not debug the executor.')
    return get_executor_path(package, i)


def main(
        executor_description,
        test_scenario,
        threads=3,
):
    executor_name = f'MicroChainExecutor{random.randint(0, 1000_000)}'

    packages = get_possible_packages(executor_description, threads)
    recreate_folder('executor')
    for package in packages:
        create_executor(executor_description, test_scenario, executor_name, package)
        # executor_name = 'MicroChainExecutor790050'
        executor_path = debug_executor(package, executor_description, test_scenario)
        # print('Executor can be built locally, now we will push it to the cloud.')
        # jina_cloud.push_executor(executor_path)
        print('Deploy a jina flow')
        host = jina_cloud.deploy_flow(executor_name, 'flow')
        print(f'Flow is deployed create the playground for {host}')
        create_playground(executor_name, executor_path, host)
        print(
            'Executor name:', executor_name, '\n',
            'Executor path:', executor_path, '\n',
            'Host:', host, '\n',
            'Playground:', f'streamlit run {executor_path}/app.py', '\n',
        )


def get_possible_packages(executor_description, threads):
    print_colored('', '############# What package to use? #############', 'red')
    user_query = f'''
Here is the task description of the problme you need to solve:
"{executor_description}"
First, write down all the subtasks you need to solve which require python packages.
For each subtask:
    Provide a list of 1 to 3 python packages you could use to solve the subtask.
    For each package:
        Write down some non-obvious thoughts about the challenges you might face for the task and give multiple approaches on how you handle them.
        For example, there might be some packages you must not use because they do not obay the rules:
        {not_allowed()}
        Discuss the pros and cons for all of these packages.
Create a list of package subsets that you could use to solve the task.
The list is sorted in a way that the most promising subset of packages is at the top.
The maximum length of the list is 5.

The output must be a list of lists wrapped into ``` and starting with **packages.csv** like this:
**packages.csv**
```
package1,package2
package2,package3,...
...
```
    '''
    conversation = gpt.Conversation()
    packages_raw = conversation.query(user_query)
    packages_csv_string = extract_content_from_result(packages_raw, 'packages.csv')
    packages = [package.split(',') for package in packages_csv_string.split('\n')]
    packages = packages[:threads]
    return packages


if __name__ == '__main__':
    # ######## Level 1 task #########
    # main(
    #     executor_description="The executor takes a pdf file as input, parses it and returns the text.",
    #     input_modality='pdf',
    #     output_modality='text',
    #     test_scenario='Takes https://www2.deloitte.com/content/dam/Deloitte/de/Documents/about-deloitte/Deloitte-Unternehmensgeschichte.pdf and returns a string that is at least 100 characters long',
    # )

    # main(
    #     executor_description="The executor takes a url of a website as input and returns the logo of the website as an image.",
    #     test_scenario='Takes https://jina.ai/ as input  and returns an svg image of the logo.',
    # )

    main(
        executor_description="The executor takes a url of a website as input and classifies it as either individual or business.",
        test_scenario='Takes https://jina.ai/ as input  and returns "business". Takes https://hanxiao.io/ as input and returns "individual". ',
    )

    # # # ######## Level 1 task #########
    # main(
    #     executor_description="The executor takes a pdf file as input, parses it and returns the text.",
    #     input_modality='pdf',
    #     output_modality='text',
    #     test_scenario='Takes https://www2.deloitte.com/content/dam/Deloitte/de/Documents/about-deloitte/Deloitte-Unternehmensgeschichte.pdf and returns a string that is at least 100 characters long',
    # )

    # ######## Level 2 task #########
    # main(
    #     executor_description="OCR detector",
    #     input_modality='image',
    #     output_modality='text',
    #     test_scenario='Takes https://miro.medium.com/v2/resize:fit:1024/0*4ty0Adbdg4dsVBo3.png as input and returns a string that contains "Hello, world"',
    # )

    # ######## Level 3 task #########
    # main(
    #     executor_description="The executor takes an mp3 file as input and returns bpm and pitch in a json.",
    #     input_modality='audio',
    #     output_modality='json',
    #     test_scenario='Takes https://miro.medium.com/v2/resize:fit:1024/0*4ty0Adbdg4dsVBo3.png as input and returns a json with bpm and pitch',
    # )

    ######### Level 4 task #########
    # main(
    #     executor_description="The executor takes 3D objects in obj format as input "
    #                          "and outputs a 2D image projection of that object where the full object is shown. ",
    #     input_modality='3d',
    #     output_modality='image',
    #     test_scenario='Test that 3d object from https://raw.githubusercontent.com/polygonjs/polygonjs-assets/master/models/wolf.obj '
    #                   'is put in and out comes a 2d rendering of it',
    # )

    # ######## Level 8 task #########
    # main(
    #     executor_description="The executor takes an image as input and returns a list of bounding boxes of all animals in the image.",
    #     input_modality='blob',
    #     output_modality='json',
    #     test_scenario='Take the image from https://thumbs.dreamstime.com/b/dog-professor-red-bow-tie-glasses-white-background-isolated-dog-professor-glasses-197036807.jpg as input and assert that the list contains at least one bounding box. ',
    # )