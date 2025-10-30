"""
Python script to be called by a GitHub action.

This script parses the Github Issue JSON contained in the GITHUB_CONTEXT
environment variable. It parses this content and creates the necessary
Blueprint, and all the associated files.
"""

from collections.abc import Iterable, Mapping
from datetime import datetime
from json import dump as json_dump, loads, JSONDecodeError
from os import environ
from pathlib import Path
from re import compile as re_compile
from shutil import copy as file_copy, unpack_archive, ReadError
from sys import exit as sys_exit
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any, TypedDict

from requests import get

from src.database.db import add_to_set, create_new_blueprint, create_new_set
from src.build.helper import get_blueprint_folders


ROOT = Path(__file__).parent.parent.parent
BLUEPRINT_FOLDER = ROOT / 'blueprints'
TEMP_PREVIEW_FILE = ROOT / 'preview.jpg'
TEMP_FILE = ROOT / 'tmp'
TEMP_DIRECTORY = ROOT / 'unzipped'
URL_REGEX = re_compile(r'\!?\[.*?\]\(([^\s]+)\)')


class BlueprintSubmission(TypedDict):
    series_name: str
    series_year: int
    database_ids: dict
    creator: str
    preview_urls: list[str]
    font_zip_url: str | None
    source_file_zip_url: str | None
    set_ids: Iterable[int]
    blueprint: dict

class SetSubmission(TypedDict):
    name: str
    blueprint_paths: list[str]


def parse_database_ids(ids: str) -> dict[str, int | str]:
    """
    Parse the given database ID strings into a dictionary of database
    IDs.

    >>> parse_database_ids('imdb:tt1234')
    {'imdb': 'tt1234'}
    >>> parse_database_ids('imdb:tt9876,tmdb:1234')
    {'imdb': 'tt9876', 'tmdb': 1234}
    """

    # No IDs specified, return empty dictionary
    if '_No response_' in ids or not ids:
        return {}

    # Parse each comma-separated ID
    database_ids: dict[str, int | str] = {}
    for id_substr in ids.split(','):
        try:
            id_type, id_ = id_substr.strip().split(':')
            id_ = str(id_).strip()
        except ValueError as exc:
            print(f'Invalid database IDs {exc}')
            continue

        # Store as int if all digits, otherwise str
        database_ids[id_type] = int(id_) if id_.isdigit() else id_

    return database_ids


def parse_urls(raw: str | None) -> list[str]:
    """
    Parse the raw markdown into a list of URLs.

    >>> parse_urls('![preview](example.jpg) ![preview](example2.jpg)')
    ['example.jpg', 'example2.jpg']
    >>> parse_urls('[zip](https://fonts.zip)')
    ['https://fonts.zip']

    Args:
        raw: Raw Markdown of embedded file links to parse. Should be
            pulled directly from the Issue template.

    Returns:
        List of URLs.
    """

    return [] if raw is None else URL_REGEX.findall(raw)


def parse_bp_submission(
        data: dict | None = None,
        *,
        environment: Mapping[str, Any] = environ,
    ) -> BlueprintSubmission:
    """
    Parse the submission from the `ISSUE_BODY` and `ISSUE_CREATOR`
    environment variables into a dictionary of submission data.

    Args:
        data: Data set to use instead of the environment variable. For
            manual importing.
        environment: Environment map to query data from.

    Returns:
        Data (as a dictionary) of the given submission.
    """

    # Parse issue from environment variable
    if data is None:
        try:
            content = loads(environment.get('ISSUE_BODY'))
            print(f'Parsed issue JSON as:\n{content}')
        except JSONDecodeError as exc:
            print(f'Unable to parse Context as JSON')
            print(exc)
            sys_exit(1)

        # Get the issue's author and the body (the issue text itself)
        creator = environment.get('ISSUE_CREATOR', 'CollinHeist')

        # Extract the data from the issue text
        issue_regex = re_compile((
            r'^'
            r'### Series Name\s+(?P<series_name>.+)\s+'
            r'### Series Year\s+(?P<series_year>\d+)\s+'
            r'### Series Database IDs\s+(?P<database_ids>.+)\s+'
            r'### Creator Username\s+(?P<creator>.+)\s+'
            r'### Blueprint Description\s+(?P<description>[\s\S]*?)\s+'
            r'### Blueprint\s+```json\s+(?P<blueprint>[\s\S]*?)```\s+'
            r'### Preview Title Cards\s+.*?(?P<preview_urls>[\s\S]*?)\s+'
            r'### Zip of Font Files\s+(_No response_|\[.+?\]\((?P<font_zip>http[^\s\)]+)\))\s+'
            r'### Zip of Source Files\s+(_No response_|\[.+?\]\((?P<source_files>http[^\s\)]+)\))\s+'
            r'### Set IDs\s+(_No response_|(?P<set_ids>[\d,]+))\s*$'
        ))

        # If data cannot be extracted, exit
        if not (data := issue_regex.match(content)):
            print(f'Unable to parse Blueprint from Issue')
            print(f'{content=!r}')
            sys_exit(1)
        data = data.groupdict()

    # Get each variable from the issue
    print(f'Raw Data: {data=}')
    data = {
        'font_zip': '_No response_',
        'source_files': '_No response_',
        'set_ids': '_No response_',
    } | data

    creator = (creator if '_No response_' in data['creator'] else data['creator']).strip()
    if data.get('font_zip') is None or '_No response_' in data['font_zip']:
        font_zip_url = None
    else:
        font_zip_url = data['font_zip']
    if data.get('source_files') is None or '_No response_' in data['source_files']:
        source_files = None
    else:
        source_files = data['source_files']
    if data.get('set_ids') is None or '_No response_' in data['set_ids']:
        set_ids = []
    else:
        set_ids: list[int] = list(map(int, data['set_ids'].split(',')))

    # Parse blueprint as JSON
    try:
        blueprint = loads(data['blueprint'])
    except JSONDecodeError:
        print(f'Unable to parse blueprint as JSON')
        print(f'{data["blueprint"]=!r}')
        sys_exit(1)

    # Clean up description
    description = [
        line.strip() + ('' if line.strip().endswith('.') else '.')
        for line in data['description'].splitlines()
        if line.strip()
    ]

    return {
        'series_name': data['series_name'].strip(),
        'series_year': int(data['series_year']),
        'database_ids': parse_database_ids(data['database_ids']),
        'creator': creator,
        'preview_urls': parse_urls(data['preview_urls']),
        'font_zip_url': font_zip_url,
        'source_file_zip_url': source_files,
        'set_ids': set_ids,
        'blueprint': blueprint | {
            'creator': creator,
            'description': description,
        }
    }


def download_preview(url: str, index: int, blueprint_subfolder: Path):
    """
    Download the preview image at the given URL and write it to the
    Blueprint folder. This writes the image as `preview{index}.jpg`.

    Args:
        url: URL to the preview file to download.
        index: Index number of this preview
        blueprint_subfolder: Subfolder of the Blueprint to download the
            preview file into.
    """

    # Download preview
    if not (response := get(url, timeout=30)).ok:
        print(f'Unable to download preview file from "{url}"')
        print(response.content)
        sys_exit(1)

    # Copy preview into blueprint folder
    file = blueprint_subfolder / f'preview{index}.jpg'
    _ = file.write_bytes(response.content)
    print(f'Downloaded "{url}" into "{file.resolve()}"')


def download_zip(zip_url: str, blueprint_subfolder: Path) -> list[Path]:
    """
    Download any files in the ZIP located at the given URL and write
    them in the given Blueprint folder.

    Args:
        zip_url: URL to the zip file to download.
        blueprint_subfolder: Subfolder of the Blueprint to download
            and unpack the zip files into.

    Returns:
        List of the downloaded files.
    """

    if not zip_url:
        return []

    # Download from URL
    if not (response := get(zip_url, timeout=30)).ok:
        print(f'Unable to download zip from "{zip_url}"')
        print(response.content)
        sys_exit(1)
    print(f'Downloaded "{zip_url}"')

    # Write zip to temporary file
    files = []
    extension = zip_url.rsplit('.', maxsplit=1)[-1]
    with NamedTemporaryFile(suffix=f'.{extension}') as file_handle:
        _ = file_handle.write(response.content)

        # Unpack zip into temporary folder
        with TemporaryDirectory() as directory:
            try:
                unpack_archive(file_handle.name, directory)
                print(f'Unpacked zip into "{directory}"')
            except (ValueError, ReadError):
                print(f'Unable to unzip files from "{zip_url}"')
                sys_exit(1)

            for file in Path(directory).glob('**/*'):
                if file.is_dir():
                    print(f'Skipping [zip]/{file} - is a directory')
                    continue
                if file.name.startswith('._'):
                    print(f'Skipping [zip]/{file} - starts with "._"')
                    continue

                destination = blueprint_subfolder / str(file.name)
                _ = file_copy(file, destination)
                files.append(destination)
                print(f'Copied [zip]/{file.name} into "{blueprint_subfolder}"')

    return files


def parse_and_create_blueprint():
    """
    Parse the Blueprint submission from the environment variables, add
    the resulting Series and Blueprint to the Blueprints database, and
    write the Blueprint files to the appropriate Blueprint subfolder(s).
    """

    # Parse submission, get associated Series and Blueprint SQL objects
    submission = parse_bp_submission()
    print(f'{"-"*25}\n{submission=}\n{"-"*25}')
    fallback_path_name = get_blueprint_folders(
        f'{submission["series_name"]} ({submission["series_year"]})'
    )[1]
    series, blueprint = create_new_blueprint(
        submission['series_name'], submission['series_year'],
        fallback_path_name, submission['database_ids'], submission['creator'],
        submission['blueprint'],
    )

    # Add to any associated Sets
    for set_id in submission['set_ids']:
        _ = add_to_set(set_id, blueprint)

    # Get the associated folder for this Series
    letter, folder_name = get_blueprint_folders(f'{series.name} ({series.year})')

    # Create Series folder
    series_subfolder = BLUEPRINT_FOLDER / letter / folder_name
    series_subfolder.mkdir(exist_ok=True, parents=True)

    # Create Blueprint ID folder
    blueprint_subfolder = series_subfolder / str(blueprint.blueprint_number)
    blueprint_subfolder.mkdir(exist_ok=True, parents=True)
    print(f'Created blueprints/{letter}/{folder_name}/{blueprint.blueprint_number}')

    # Download preview
    for index, preview in enumerate(submission['preview_urls']):
        download_preview(preview, index, blueprint_subfolder)

    # Add preview image to blueprint
    submission['blueprint']['previews'] = [
        f'preview{index}.jpg'
        for index, _ in enumerate(submission['preview_urls'])
    ]

    # Download any font zip files if provided
    download_zip(submission['font_zip_url'], blueprint_subfolder)

    # Download source files
    source_files = download_zip(
        submission['source_file_zip_url'], blueprint_subfolder
    )

    # Add source files to Blueprint
    if source_files:
        submission['blueprint']['series']['source_files'] = [
            file.name for file in source_files
        ]

    # Add creation time to Blueprint
    submission['blueprint']['created'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')

    # Write Blueprint as JSON
    blueprint_file = blueprint_subfolder / 'blueprint.json'
    with blueprint_file.open('w') as file_handle:
        json_dump(submission['blueprint'], file_handle, indent=2)
    print(f'Wrote Blueprint at blueprints/{letter}/{folder_name}/{blueprint.blueprint_number}/blueprint.json')
    print(f'{"-" * 25}\n{submission["blueprint"]}\n{"-" * 25}')


def _parse_set_submission(
        *,
        environment: Mapping[str, Any] = environ
    ) -> SetSubmission:
    """
    Parse a Blueprint Set submission from the given environment, and
    return the "sanitized" submission data.

    Args:
        environment: Environment map to query data from.

    Returns:
        Dictionary of the submission data.
    """

    # Parse issue from environment variable
    try:
        content: str = loads(environment.get('ISSUE_BODY'))
        print(f'Parsed issue JSON as:\n{content}')
    except JSONDecodeError as exc:
        print(f'Unable to parse issue as JSON')
        print(exc)
        sys_exit(1)

    # Extract the data from the issue text
    issue_regex = re_compile((
        r'^'
        r'### Set Name\s+(?P<set_name>.+)\s+'
        r'### Blueprints\s+(?P<blueprints>[\s\S]*)$'
    ))

    # If data cannot be extracted, exit
    if not (data := issue_regex.match(content)):
        print(f'Unable to parse Set from JSON')
        print(f'{content=!r}')
        sys_exit(1)

    data = data.groupdict()
    print(f'Raw Data: {data=}')

    def _parse_path(path: str, /) -> str:
        """Turn GitHub URLs into paths"""
        return path.strip().removeprefix(
            'https://github.com/TitleCardMaker/Blueprints/tree/master/'
        )

    return {
        'name': data['set_name'].strip(),
        'blueprint_paths': list(map(
            _parse_path,
            data['blueprints'].strip().splitlines()
        ))
    }


def parse_blueprint_set():
    """
    Parse the Set submission from the environment variables and add the
    the resulting Set to the database.
    """

    submission = _parse_set_submission()
    print(f'{"-"*25}\n{submission=}\n{"-"*25}')

    bp_set = create_new_set(**submission)
    bp_ids = [blueprint.id for blueprint in bp_set.blueprints]
    print((
        f'Created Set "{bp_set.name}" with {len(bp_set.blueprints)} '
        f'Blueprints{bp_ids}'
    ))
