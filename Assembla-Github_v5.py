"""
See: https://www.codeproject.com/Articles/5247083/Migrating-from-Assembla-to-Github-using-Automation

Export .bak file from Assembla, then manual regexp find and replace:

    # Replace IC repo with Syrinscape repo.
    ixc/syrinscape/(?=blame|blob|commit|compare|tree) -> syrinscape/syrinscape/

Delete GitHub issues:

    time python Assembla-Github_v5.py --delete --repo syrinscape/syrinscape

Download files from Assembla:

    time python Assembla-Github_v5.py --download

Rename downloaded files from Assembla:

    time python Assembla-Github_v5.py --rename

Upload downloaded files to GitHub:

    time python Assembla-Github_v5.py --upload --repo syrinscape/syrinscape

NOTE: GitHub will randomly complain that "Something went really wrong..." Just wait a
while and try again. It appears to be an undocumented rate limit.

Copy the `data/files` directory to a location accessible via `FILES_URL`. Any files that
cannot be uploaded to GitHub will be linked to there.

Create GitHub issues:

    time python Assembla-Github_v5.py --repo syrinscape/syrinscape

Update existing GitHub issues (e.g. after a new Assembla export):

    time python Assembla-Github_v5.py --update --repo syrinscape/syrinscape

"""

import calendar
import ipdb
import time

from github import Github
from github.GithubException import RateLimitExceededException
import ast, os, sys, glob, io, requests, zipfile
from sys import exit
from time import sleep
from datetime import datetime
from selenium.webdriver.chrome.options import Options
from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException, TimeoutException, NoSuchElementException, JavascriptException
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
from argparse import ArgumentParser
from credentials import Credentials
import regex as re


# RETRY ON RATE LIMIT ##################################################################

# See: https://github.com/PyGithub/PyGithub/issues/2113#issuecomment-1008288358

import json
import logging

from github import GithubException
from requests import Response
from requests.models import CaseInsensitiveDict
from requests.utils import get_encoding_from_headers
from urllib3 import Retry, HTTPResponse
from urllib3.exceptions import MaxRetryError

# from publish.github_action import GithubAction

logger = logging.getLogger(__name__)


class GitHubRetry(Retry):
    # gha: GithubAction = None

    def __init__(self, **kwargs):
        # if 'gha' in kwargs:
        #     self.gha = kwargs['gha']
        #     del kwargs['gha']

        # 403 is too broad to be retried, but GitHub API signals rate limits via 403
        # we retry 403 and look into the response header via Retry.increment
        kwargs['status_forcelist'] = kwargs.get('status_forcelist', []) + [403]
        super().__init__(**kwargs)

    # def new(self, **kw):
    #     retry = super().new(**kw)
    #     # retry.gha = self.gha
    #     return retry

    def increment(self,
                  method=None,
                  url=None,
                  response=None,
                  error=None,
                  _pool=None,
                  _stacktrace=None):
        if response:
            logger.warning(f'Request {method} {url} failed: {response.reason}')
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f'Response headers:')
                for field, value in response.headers.items():
                    logger.debug(f'- {field}: {value}')

            # we retry 403 only if there is a Retry-After header (indicating it is retry-able)
            # or if the body message implies so
            if response.status == 403:
                # self.gha.warning(f'Request {method} {url} failed with 403: {response.reason}')
                if 'Retry-After' in response.headers:
                    logger.info(f'Retrying after {response.headers.get("Retry-After")} seconds')
                else:
                    logger.info(f'There is no Retry-After given in the response header')
                    content = response.reason
                    try:
                        content = get_content(response, url)
                        content = json.loads(content)
                        message = content.get('message', '').lower()

                        if (
                            message.startswith('api rate limit exceeded')
                            or message.endswith('please wait a few minutes before you try again.')
                            or message.endswith('please retry your request again later.')
                        ):
                            logger.info('Response body indicates retry-able error')
                            return super().increment(method, url, response, error, _pool, _stacktrace)

                        logger.info('Response message does not indicate retry-able error')
                    except MaxRetryError:
                        raise
                    except Exception as e:
                        logger.warning('failed to inspect response message', exc_info=e)

                    raise GithubException(response.status, content, response.headers)

        return super().increment(method, url, response, error, _pool, _stacktrace)


def get_content(resp: HTTPResponse, url: str):
    # logic taken from HTTPAdapter.build_response (requests.adapters)
    response = Response()

    # Fallback to None if there's no status_code, for whatever reason.
    response.status_code = getattr(resp, 'status', None)

    # Make headers case-insensitive.
    response.headers = CaseInsensitiveDict(getattr(resp, 'headers', {}))

    # Set encoding.
    response.encoding = get_encoding_from_headers(response.headers)
    response.raw = resp
    response.reason = response.raw.reason

    response.url = url

    return response.content

# END RETRY ON RATE LIMIT ##############################################################


retry = GitHubRetry(total=100,
                    backoff_factor=1,
                    allowed_methods=Retry.DEFAULT_ALLOWED_METHODS.union({'GET', 'POST'}),
                    status_forcelist=list(range(500, 600)))
g = Github(Credentials.github_user, Credentials.github_token, retry=retry)


def github_check_rate_limit(core=1, graphql=1, search=1):
    """
    Sleep until the rate limit is reset if we have fewer than the required number of
    requests (core, graphql, search - default: 1) remaining.
    """
    rate_limit = g.get_rate_limit()
    for limit, required in (
        (rate_limit.core, core),
        (rate_limit.graphql, graphql),
        (rate_limit.search, search),
    ):
        if limit.remaining < required:
            reset_timestamp = calendar.timegm(limit.reset.timetuple())
            # Sleep until 1 second past the reset.
            sleep_time = reset_timestamp - calendar.timegm(time.gmtime()) + 1
            print(
                "GitHub API rate limit nearly depleted (%s/%s). Sleeping for %s "
                "seconds. %r" % (
                    required, limit.remaining, sleep_time, limit
                )
            )
            time.sleep(sleep_time)


def github_iter(seq):
    """
    Iterate a paginated sequence while avoiding the API rate limit.
    """
    new_seq = []
    github_check_rate_limit()
    for i, item in enumerate(seq, start=1):
        if i == len(seq._PaginatedListBase__elements) and seq._couldGrow():
            github_check_rate_limit()
        new_seq.append(item)
    return new_seq


COMMITS = []
FILES_DIR = "files"
FILES_URL = "https://files.syrinscape.com/"


# Regexp substitutions to be performed in sequence to make Assembla formatted text
# compatible with GitHub Flavoured Markdown.
RE_SUB_LIST = dict(
    (
        # Replace unicode angle brackets with ascii, to make the following regexps easier.
        (r"\u003c", r"<"),
        (r"\u003e", r">"),

        # Fix invalid closing tag(s).
        (r"<\\", r"</"),

        # Fix invalid file link.
        (r"(?<!\[)file:aqN1LyirOr5ioYacwqjQXA", r"[[file:aqN1LyirOr5ioYacwqjQXA.zip]]"),

        # Convert opening and closing <pre> and <code> tags to distinctive unicode
        # characters so we can use them in regexp character classes below.
        (r"<pre>", r"¢"),
        (r"</pre>", r"µ"),
        (r"<code>", r"±"),
        (r"</code>", r"¾"),

        # Add backtics to HTML tags appearing outside preformatted and inline code
        # blocks because GitHub won't escape them.
        (r"""
            (?sx)               # Multiline, verbose
            (?:                 # Non-caturing group
                (?<![¢±].*)     # Look behind to NOT find <pre> or <code>
                |               # OR
                (?<=[¾µ][^¢±]*) # Look behind to find </code> or </pre> NOT followed by
                                # <pre> or <code>
            )
            (?<!`)              # Look behind to NOT find a backtic
            (<[^>]+>)           # Capture any HTML tag
            (?!`)               # Look ahead to NOT find a backtic
        """, r"`\1`"),

        # Replace <code> tags with backtics for inline code blocks (not fenced code
        # blocks) because GitHub won't escape them.
        (r"(?<!¢)±([^¾\n]+)¾?", r"`\1`"),

        # Convert opening and closing pre/code tags back to their original form.
        (r"¢", r"<pre>"),
        (r"µ", r"</pre>"),
        (r"±", r"<code>"),
        (r"¾", r"</code>"),

        # Add fences to preformatted code blocks because GitHub won't escape them.
        (r"(\n| )*<pre>(\n| )*(<code>(\n)*)?", r"\n\n```\n"),
        (r"((\n)*</code>)?(\n| )*</pre>(\n| )*", r"\n```\n\n"),

        # Replace gremlins.
        (r" ", r" "),         # Space
        (r"‎", r""),   # Empty string
        (r"‪", r"["),  # Open bracket
        (r"‬", r"]"),  # Close bracket
        (r"–", r"-"),         # Dash
        (r"[‘’]", r"'"),      # Single quote
        (r"[“”]", r'"'),      # Double quote
    )
)


def assembla_to_gfm(text):
    reserved = re.search(r"[¢µ±¾]", text)
    assert not reserved, (
        f"Found reserved unicode character {reserved.group()} in text: {text}"
    )
    for pat, sub in RE_SUB_LIST.items():
        text = re.sub(pat, sub, text)
    return text


before = """
An <h1> on its own, and <code>an inline code block with <b>bold <i>and italic</i></b> text</code>, <i>and</i> <pre>a <i>preformatted</i> block</pre> <b>and</b> <pre><code>

a preformatted
<i>code block</i>

</code></pre> and an <i>unclosed</i> <pre><code>preformatted <b>code block</b>
"""

after = """
An `<h1>` on its own and `an inline code block with <b>bold <i>and italic</i></b> text`, `<i>`and`</i>`

```
a <i>preformatted</i> block
```

`<b>`and`</b>`

```
a preformatted
<i>code block</i>
```

and an `<i>`unclosed`</i>`

```
preformatted <b>code block</b>
"""

# assert assembla_to_gfm(before) == after, "Regexp test failure."


val = {
# add here your user and space_id of each contributor separated with a comma - for example
    'addyyeow': 'aOZlLIPOur5kejacwqjQYw',
    'aliceathens': 'a5cwOeFaSr5ioBacwqjQXA',
    'Anonymous': 'bgfq4qA1Gr2QjIaaaHk9wZ',
    'Aramgutang': 'bjBk7u6nWr3lUBabIlDkbG',
    'Arianne.E': 'cRDJM0VPGr4OkNacwqjQWU',
    'aweakley': 'di0DGS4Z8r3inTabIlDkbG',
    'BenjaminLoomes': 'buE2R2Ooar4ONdacwqEsg8',
    'cogat': 'dCkEYsHP0r3icQabIlDkbG',
    'DrMeers': 'alce22Iair366feJe5cbCb',
    'Fabianmcdonald': 'b-NzA8r-mr5RK_dmr6CpXy',
    'jamesmurty': 'agL5-eYU8r4BknacwqjQXA',
    'jonhuber': 'bdreHwH30r4OVdacwqjQXA',
    'kaveht': 'cDXunmL1ur56dcacwqEsg8',
    'lostsheep007': 'a0Fv1cp8ur5ikCacwqjQYw',
    'Mark Finger': 'cd5j9MQNGr4lvReJe5cbCb',
    'mattjg': 'bIWj8okVmr7jJccP_HzTya',
    'mattoc': 'axJJ9e0WGr4i0teJe5cbLr',
    'mike@interaction.net.au': 'aeZvakTQir4OkNacwqjQXA',
    'mjog': 'bhVijwJ4yr46e8acwqjQWU',
    'mrchantey': 'dLb_rCGiyr64kgbK8JiBFu',
    'ominousbarry': 'dL_FPIV50r7iNccK-zJOy8',
    'patarmstrong': 'dv2u8uqFir5ljwacwqEsg8',
    'rantecki': 'd30d3S97Kr4y8DacwqjQWU',
    'rl-0x0': 'buJ90CdH4r4ls7eJe5cbLr',
    'ryan.cassar': 'dGzpJ6Giyr65ddcK-zJOy8',
    'ryan.stuart': 'a-hEzew8mr5yo6dmr6bg7m',
    'sam_mi': 'bazo9all4r5iepdmr6bg7m',
    'simon@redcrowdigital.com.au': 'dMoZ4C9vur4z-OacwqjQYw',
    'sjdines': 'a637DGkwur463cacwqjQYw',
    'Syrin-Chris': 'dyQVs0V50r7k4saIC_Qgzw',
    'tailee': 'cRu3xiRvGr44oYacwqEsg8',
    'timjmansfield': 'chrW-oQZKr5jJcdmr6bg7m',
}

KB = 1000
MB = KB * 1000

# Allowed extensions.
EXTS = {
    '.docx': 25 * MB,
    '.gif': 10 * MB,
    '.gz': 25 * MB,
    '.jpeg': 10 * MB,
    '.jpg': 10 * MB,
    '.log': 25 * MB,
    '.mov': 10 * MB,
    '.mp4': 10 * MB,
    '.pdf': 25 * MB,
    '.png': 10 * MB,
    '.pptx': 25 * MB,
    '.svg': 10 * MB,
    '.txt': 25 * MB,
    '.xlsx': 25 * MB,
    '.zip': 25 * MB,
}

# Map extensions with alternate spellings to the canonical version.
EXTS_MAP = {
    '.jpeg': '.jpg',
}


def every_downloads_chrome(driver):
    # waits for download to complete
    if not driver.current_url.startswith("chrome://downloads"):
        driver.get("chrome://downloads/")
    return driver.execute_script("""
        var items = downloads.Manager.get().items_;
        if (items.every(e => e.state === "COMPLETE"))
            return items.map(e => e.fileUrl || e.file_url);
        """)


def parseAttachmentsFromBak(space_id, bak_refs):
    """
    Download refs in the `.bak` file.
    """
    link = f"https://bigfiles.assembla.com/spaces/{space_id}/documents/download/"
    existing = [get_file_id(item) for item in glob.glob(os.path.join(FILES_DIR, "**"))]
    files_to_download = [item for item in bak_refs if item not in existing]
    skipped = len(set(bak_refs)) - len(files_to_download)
    print("Skipping download for %s existing files." % skipped)
    chrome_options = webdriver.ChromeOptions()
    path = os.path.abspath(".")
    chrome_options.add_experimental_option("prefs", {
    "download.default_directory": os.path.join(path, "temp"),
    "download.prompt_for_download": False,
    "download.directory_upgrade": True,
    "safebrowsing.enabled": False,
    })
    chrome_options.add_argument("user-data-dir=selenium")
    chrome_options.add_argument("start-maximized")
    chrome_options.add_argument("--disable-infobars")
    try:
        driver = webdriver.Chrome(executable_path=ChromeDriverManager().install(), options=chrome_options, service_log_path='NUL')
    except ValueError:
        print("Error opening Chrome. Chrome is not installed?")
        exit(1)
    FILE_SAVER_MIN_JS_URL = "https://raw.githubusercontent.com/eligrey/FileSaver.js/master/src/FileSaver.js"
    file_saver_min_js = requests.get(FILE_SAVER_MIN_JS_URL).content
    driver.get("https://bigfiles.assembla.com/login")
    sleep(2)
    checklink = driver.execute_script("return document.URL;")
    if checklink == "https://bigfiles.assembla.com/login":
        login = driver.find_element_by_id("user_login")
        login.clear()
        login.send_keys(Credentials.assembla_user)
        passw = driver.find_element_by_id("user_password")
        passw.clear()
        passw.send_keys(Credentials.assembla_password)
        btn = driver.find_element_by_id("signin_button")
        btn.click()
        sleep(1)
    print(
        "Attempting to download %s files:\n  %s" % (
            len(files_to_download), "\n  ".join(files_to_download)
        )
    )
    for file in files_to_download:
        file_id = get_file_id(file)
        # fetch all files from the files_to_download
        download_script = f"""
            return fetch('{file}',
                {{
                    "credentials": "same-origin",
                    "headers": {{"accept":"*/*;q=0.8","accept-language":"en-US,en;q=0.9"}},
                    "referrerPolicy": "no-referrer-when-downgrade",
                    "body": null,
                    "method": "GET",
                    "mode": "cors"
                }}
            ).then(resp => {{
                return resp.blob();
            }}).then(blob => {{
                saveAs(blob, '{file}');
            }});
            """
        driver.get(f"{link}{file}")
        sleep(1)
        try:
            # execute FileSaver.js if content == img
            loc = driver.find_element_by_tag_name('img')
            if loc:
                driver.execute_script(file_saver_min_js.decode("ascii"))
                driver.execute_script(download_script)
                WebDriverWait(driver, 120, 1).until(every_downloads_chrome)
            WebDriverWait(driver, 120, 1).until(every_downloads_chrome)
        except TimeoutException:
            pass
        except NoSuchElementException:
            pass
        except JavascriptException:
            pass
        while True:
            sleep(8)
            crdownload_files = glob.glob(os.path.join("temp", "*.crdownload"))
            if not glob.glob(os.path.join("temp", "*.crdownload")):
                break
            print("Still downloading: %r" % crdownload_files)
        for temp_file in glob.glob(os.path.join("temp", "**")):
            temp_ext = get_extension(temp_file)
            dst = os.path.join(FILES_DIR, f"{file_id}{temp_ext}")
            print(f"Renaming file: {temp_file} -> {dst}")
# Renaming file: temp/Screen Recording 2022-07-24 at 12.35.01 pm.mov.crdownload -> files/b4RjhicVOr7yk1bK8JiBFu.35.01 pm.mov.crdownload
            try:
                os.remove(dst)
            except FileNotFoundError:
                pass
            os.rename(temp_file, dst)
    driver.close()
    driver.quit()


def parseTickets(tickets):
    print("Parsing tickets...")
    tickets_arr = []
    find_tickets = re.findall(r"^tickets,\s.*", tickets, re.MULTILINE)
    for item in find_tickets:
        arr = re.search(r"\[.*\]", item)
        fault_replace = str(arr.group(0)).replace(",null", ',"null"')
        array = ast.literal_eval(fault_replace)
        ticket_info = {
            "ticket_id": array[0],
            "ticket_number": array[1],
            "ticket_reporter_id": array[2],
            "ticket_assigned_id": array[3],
            "ticket_title": assembla_to_gfm(array[5]),
            "ticket_priority": array[6],
            "ticket_description": assembla_to_gfm(array[7]),
            "ticket_created_on": array[8],
            "ticket_milestone": array[10],
            "ticket_username": None,
            "status": "",
            "references": set(),
            "real_number": None,
            "ticket_comments": []
        }
        #["id","number","reporter_id","assigned_to_id","space_id","summary","priority","description",
        # "created_on","updated_at","milestone_id","component_id","notification_list",
        # "completed_date","working_hours","is_story","importance","story_importance","permission_type",
        # "ticket_status_id","state","estimate","total_estimate","total_invested_hours","total_working_hours",
        # "status_updated_at","due_date","milestone_updated_at"]

        # get all comments belonging to specific ticket
        find_ticket_comments = re.findall(r"^ticket_comments,\s\[\d+\,{}.*".format(ticket_info["ticket_id"]), tickets, re.MULTILINE)
        for item in find_ticket_comments:
            arr = re.search(r"\[.*\]", item)
            fault_replace = re.sub(r",null", ',"null"', str(arr.group(0)))
            # transform comment array to python array
            array = ast.literal_eval(fault_replace)
            #ticket_comments:fields, ["id","ticket_id","user_id","created_on","updated_at","comment","ticket_changes","rendered"]
            comment_info = {
                "id": array[0],
                "ticket_id": array[1],
                "user_id": array[2],
                "created_on": array[3],
                "updated_at": array[4],
                "comment": assembla_to_gfm(array[5]),
                "ticket_changes": array[6],
                "rendered": array[7],
                "attachments": [],
                "username": None
            }
            ticket_info["ticket_comments"].append(comment_info)

        sorted_comments_array = sorted(
            ticket_info["ticket_comments"],
            key=lambda x: datetime.strptime(x['created_on'], '%Y-%m-%dT%H:%M:%S.000+00:00')
        )
        ticket_info["ticket_comments"] = sorted_comments_array
        tickets_arr.append(ticket_info)
    return tickets_arr


# find statuses and link them to original ticket
def parseStatus(tickets):
    print("Parsing tickets status data...")
    find_status = re.findall(r"^ticket_changes,\s.*", tickets, re.MULTILINE)
    status_arr = []
    for item in find_status:
        #ticket_changes:fields, ["id","ticket_comment_id","subject","before","after","extras","created_at","updated_at"]
        arr = re.search(r"\[.*\]", item)
        fault_replace = str(arr.group(0)).replace(",null", ',"null"')
        array = ast.literal_eval(fault_replace)
        ticket_status_info = {
            "id": array[0],
            "ticket_comment_id": array[1],
            "subject": array[2],
            "before": array[3],
            "after": array[4],
            "extras": array[5],
            "created_at": array[6],
            "updated_at": array[7],
            "ticket_comments": []
        }
        status_arr.append(ticket_status_info)
    return status_arr

def linkStatus(sorted_tickets_array, sorted_status_array, tickets):
    print("Linking status data to tickets...")
    for tick in sorted_tickets_array:
        for stat in sorted_status_array:
            for comment in tick["ticket_comments"]:
                if comment["id"] == stat["ticket_comment_id"] and stat["subject"] == "status":
                    tick["status"] = stat["after"]
                elif comment["id"] == stat["ticket_comment_id"] and stat["subject"] == "CommentContent":
                    file_id = re.findall(r".*?\[\[(file|image):(.*?)(\|.*?)?\]\].*?", stat["before"])
                    for fi in file_id:
                        find_attach = re.findall(rf"ticket_changes,\s\[.*?\,{stat['ticket_comment_id']}\,\"attachment\"\,\"added\"\,\"(.*?)\"", tickets)
                        if find_attach:
                            for attach in find_attach:
                                comment["attachments"].append({
                                    "filename": attach,
                                    "file_id": fi[1]
                                })
                elif comment["id"] == stat["ticket_comment_id"] and stat["subject"] == "attachment":
                    if stat["before"] == "added":
                        file_id = re.findall(r".*?\[\[(file|image):(.*?)(\|.*?)?\]\].*?", comment["comment"])
                        for fi in file_id:
                            comment["attachments"].append({
                                "filename": stat["after"],
                                "file_id": fi[1]
                            })
    for tick in sorted_tickets_array:
        for comment in tick["ticket_comments"]:
            file_id = re.findall(r".*?\[\[(file|image):(.*?)(\|.*?)?\]\].*?", comment["comment"])
            if file_id:
                for fi in file_id:
                    fname = fi[1]
                    if fi[2] and fi[2] != None:
                        fname = fi[2].strip('|')
                    comment["attachments"].append({
                        "filename": fname,
                        "file_id": fi[1]
                    })
    for tick in sorted_tickets_array:
        file_id = re.findall(r".*?\[\[(file|image):(.*?)(\|.*?)?\]\].*?", tick["ticket_description"])
        if file_id:
            for fi in file_id:
                fname = fi[1]
                if fi[2] and fi[2] != None:
                    fname = fi[2].strip('|')
                comment["attachments"].append({
                    "filename": fname,
                    "file_id": fi[1]
                })
    return sorted_tickets_array, sorted_status_array


def renameFiles(sorted_tickets_array):
    print("Renaming files...")
    for item in sorted_tickets_array:
        for comment in item["ticket_comments"]:
            if comment["attachments"]:
                for attach in comment["attachments"]:
                    file_id = attach["file_id"]
                    filename = attach["filename"]
                    # Get file with extension.
                    try:
                        file = glob.glob(os.path.join(FILES_DIR, f"{file_id}.*"))[0]
                    except IndexError:
                        # Fallback to get file without extension.
                        try:
                            file = glob.glob(os.path.join(FILES_DIR, f"{file_id}"))[0]
                        except IndexError:
                            print(f"Not found file_id: {file_id}, filename: {filename}")
                            continue
                    file_ext = get_extension(file)
                    filename_ext = get_extension(filename) or file_ext
                    dst = os.path.join(FILES_DIR, f"{file_id}{filename_ext}")
                    if file == dst:
                        continue  # Nothing to do
                    try:
                        os.remove(dst)
                    except FileNotFoundError:
                        pass
                    print(f"Renaming file: {file} -> {dst}")
                    os.rename(file, dst)


def get_extension(filename):
    """
    Return canonical version of known extensions, or everything after the first dot.
    """
    _, ext = os.path.splitext(filename.lower())
    return EXTS_MAP.get(ext, ext)


def get_file_id(filename):
    """
    Strip extension from basename to return Assembla ID.
    """
    return re.sub(r"\..*", "", os.path.basename(filename))


def get_files_with_ref(files, bak_refs):
    """
    Return a filtered list of files with matching refs in the `.bak` file.
    Skip existing in `files.txt`.
    """
    files_with_ref = []
    # Get existing.
    existing = set()
    if os.path.isfile('files.txt'):
        with open('files.txt', 'r') as files_txt:
            files_txt = files_txt.read()
        existing.update(re.findall(r'\[(.*?)\]', files_txt))
        existing.update(re.findall(r'alt="(.*?)"', files_txt))
    existing = [get_file_id(item) for item in existing]
    # Get files with a matching ref. Skip existing.
    for file in files:
        file_id = get_file_id(file)
        if file_id in existing:
            continue
        if file_id in bak_refs:
            files_with_ref.append(os.path.join(os.path.abspath(FILES_DIR), file))
    return files_with_ref


def uploadToSyrinscape(files, bak_refs):
    ready_files = ""
    files_with_ref = get_files_with_ref(files, bak_refs)
    if not files_with_ref:
        print("uploadToSyrinscape(): Nothing to upload.")
        return ready_files
    for file in files_with_ref:
        # TODO: Upload.
        basename = os.path.basename(file)
        url = f'{FILES_URL}{basename}'
        line = f'[{basename}]({url})\n'
        with open('files.txt', 'a+') as files_txt:
            files_txt.write(line)
        ready_files += line
    return ready_files


def uploadToGithub(files, bak_refs, working_repo):
    ready_files = ""
    files_with_ref = get_files_with_ref(files, bak_refs)
    if not files_with_ref:
        print("uploadToGithub(): Nothing to upload.")
        return ready_files
    # launch selenium to upload attachments to github camo
    chrome_options = Options()
    chrome_options.add_argument("user-data-dir=selenium")
    chrome_options.add_argument("start-maximized")
    chrome_options.add_argument("--disable-infobars")
    try:
        driver = webdriver.Chrome(executable_path=ChromeDriverManager().install(), options=chrome_options, service_log_path='NUL')
    except ValueError:
        print("Error opening Chrome. Chrome is not installed?")
        exit(1)
    driver.implicitly_wait(1000)
    driver.get(f"https://github.com/login")
    sleep(2)
    link = driver.execute_script("return document.URL;")
    if link == "https://github.com/login":
        login = driver.find_element_by_id("login_field")
        login.clear()
        login.send_keys(Credentials.github_user)
        passw = driver.find_element_by_id("password")
        passw.clear()
        passw.send_keys(Credentials.github_password)
        btn = driver.find_elements_by_xpath("//*[@class='btn btn-primary btn-block js-sign-in-button']")
        btn[0].click()
        sleep(1)
    driver.get(f"https://github.com/{working_repo}/issues/")
    sleep(2)
    findButton = driver.find_elements_by_xpath("//*[@class='btn btn-primary']")
    findButton[0].click()
    sleep(2)
    # split files_with_ref into chunks of 2 files
    # TODO: Chunk size 1 to try and avoid "Something went really wrong, and we can't
    # process that file. Try again." error? Detect that error and actually try again?
    chunks = [files_with_ref[i:i + 2] for i in range(0, len(files_with_ref), 2)]
    for chunk in chunks:
        chk = (' \n ').join(chunk)
        findBody = driver.find_element_by_id("issue_body")
        findBody.clear()
        findButton = driver.find_element_by_id("fc-issue_body")
        findButton.clear()
        if chk:
            findButton.send_keys(chk)
        print("Waiting for uploads to finish...")
        sleep(1)
        while True:
            chk = findBody.get_attribute('value')
            # [Uploading czo0qWjmmr5PZcdmr6CpXy.zip…]()
            if "]()" in chk:
                sleep(1)
            else:
                break
        # dump ready links with attachments to a separate file
        with open('files.txt', 'a+') as ff:
            ff.write(chk)
        ready_files += chk
    driver.close()
    driver.quit()
    return ready_files

def deleteIssues(working_repo):
    chrome_options = Options()
    chrome_options.add_argument("user-data-dir=selenium")
    chrome_options.add_argument("start-maximized")
    chrome_options.add_argument("--disable-infobars")
    try:
        driver = webdriver.Chrome(executable_path=ChromeDriverManager().install(), options=chrome_options, service_log_path='NUL')
    except ValueError:
        print("Error opening Chrome. Chrome is not installed?")
        exit(1)
    driver.implicitly_wait(1000)
    driver.get(f"https://github.com/login")
    sleep(2)
    link = driver.execute_script("return document.URL;")
    if link == "https://github.com/login":
        login = driver.find_element_by_id("login_field")
        login.clear()
        login.send_keys(Credentials.github_user)
        passw = driver.find_element_by_id("password")
        passw.clear()
        passw.send_keys(Credentials.github_password)
        btn = driver.find_elements_by_xpath("//*[@class='btn btn-primary btn-block js-sign-in-button']")
        btn[0].click()
        sleep(1)

    github_check_rate_limit()
    repo = g.get_repo(working_repo)

    for issue in github_iter(repo.get_issues(state='all')):
        # GitHub's REST API v3 considers every pull request an issue, but not every
        # issue is a pull request. We cannot delete pull requests, so we must skip them.
        # See: https://docs.github.com/en/rest/issues/issues#get-an-issue
        if issue.pull_request:
            print("Skipping pull request: %s" % issue.id)
            continue
        driver.get(issue.html_url)
        # Delete issue (sidebar)
        find_button = driver.find_element_by_xpath("//*[@class='details-reset details-overlay details-overlay-dark js-delete-issue']")
        find_button.click()
        sleep(1)
        # Delete this issue (popup confirmation)
        find_button = driver.find_element_by_xpath("//*[@class='btn btn-danger input-block float-none']")
        find_button.click()
        sleep(1)

    driver.close()
    driver.quit()

def createIssue(issue_name, ticket, repo, file_links):
    labs = []
    issue = None
    if ticket["ticket_priority"] == 3:
        labs.append("Low")
    elif ticket["ticket_priority"] == 2:
        labs.append("Normal")
    else:
        labs.append("Highest")
    if ticket["status"] == "Accepted":
        labs.append("Accepted")
    if ticket["status"] == "New":
        labs.append("New")
    if ticket["status"] == "Test":
        labs.append("Test")
    if ticket['ticket_created_on'] == None:
        ticket['ticket_created_on'] = ''
    if ticket['ticket_username'] == None:
        ticket['ticket_username'] = ''
    issue_body = '**'+ticket["ticket_created_on"]+'**'+'\n'+'**'+ticket['ticket_username']+'**:'+'\n'+ticket["ticket_description"]
    search_for_hashtag = re.findall(r"(.*?)?\s?(\#\d+)", issue_body)
    # looks for revisions
    if search_for_hashtag:
        for ref in search_for_hashtag:
            try:
                if ref[0].endswith('revision'):
                    print(ref[0])
                    for c, commit in enumerate(COMMITS, start=1):
                        if c == int(ref[1].strip('#')):
                            hs = commit.sha
                            issue_body = re.sub(f"({ref[1]})", str(hs), issue_body)
            except:
                pass
    search_for_reference = re.findall(r"\[\[(.*?):(\d+)\]\].*?", issue_body)
    if search_for_reference:
        for ref in search_for_reference:
            try:
                print("Ref: ", ref)
                for c, commit in enumerate(COMMITS, start=1):
                    if c == int(ref[1]):
                        hs = commit.sha
                        issue_body = re.sub(rf"\[\[(.*?)({ref[1]})\]\].*?", hs, issue_body)
            except:
                pass
    find_urls = re.findall(r"\[\[(url):(.*?)\|(.*?)\]\].*?", issue_body)
    if find_urls:
        issue_body = re.sub(r"\[\[(url):(.*?)\]\].*?", f"[{find_urls[0][2]}]({find_urls[0][1]})", issue_body)
    issue_file = re.findall(r"\[\[(file|image):(.*?)(\|.*?)?\]\].*?", issue_body)
    if issue_file:
        for iss_file in issue_file:
            counter = 0
            for tup in file_links:
                #  check for .zip and .docx
                if tup[0] in iss_file[1]:
                    issue_body = re.sub(rf"\[\[{iss_file[0]}:{iss_file[1]}(\|.*?)?\]\]", f"![{tup[0]}]({tup[1]})", issue_body)
                elif tup[0][:-4] in iss_file[1] or tup[0][:-5] in iss_file[1]:
                    issue_body = re.sub(rf"\[\[{iss_file[0]}:{iss_file[1]}(\|.*?)?\]\]", f"[{tup[0]}]({tup[1]})", issue_body)
                else:
                    counter += 1
            if counter == len(file_links):
                issue_body = re.sub(rf"\[\[{iss_file[0]}:{iss_file[1]}(\|.*?)?\]\]", f"[file:{iss_file[1]}]", issue_body)
    if issue_body:
        github_check_rate_limit()
        issue = repo.create_issue(title=issue_name, body=issue_body, labels=labs)
        print(f"Created issue: {issue_name}")

    return issue

def addComments(ticket, issue, file_links, repo):
    github_check_rate_limit(core=2)  # Create, edit
    for comment in ticket["ticket_comments"]:
        comment_body = comment["comment"]
        if len(comment_body) == 0 or comment_body == 'null':
            pass
        else:
            if comment['created_on'] == None:
                comment['created_on'] = ''
            if comment['username'] == None:
                comment['username'] = ''
            comment_body = '**'+comment['created_on']+'**'+'\n'+'**'+comment['username']+'**:'+'\n'+comment["comment"]
            search_for_hashtag = re.findall(r"(.*?)?\s?(\#\d+)", comment_body)
            # looks for revisions
            if search_for_hashtag:
                for ref in search_for_hashtag:
                    try:
                        if ref[0].endswith('revision'):
                            print(ref[0])
                            for c, commit in enumerate(COMMITS, start=1):
                                if c == int(ref[1].strip('#')):
                                    hs = commit.sha
                                    comment_body = re.sub(f"({ref[1]})", str(hs), comment_body)
                    except:
                        pass
            search_for_reference = re.findall(r"\[\[(.*?):(\d+)\]\].*?", comment_body)
            if search_for_reference:
                for ref in search_for_reference:
                    try:
                        print("Ref: ", ref)
                        for c, commit in enumerate(COMMITS, start=1):
                            if c == int(ref[1]):
                                hs = commit.sha
                                comment_body = re.sub(rf"\[\[(.*?)({ref[1]})\]\].*?", hs, comment_body)
                    except:
                        pass
            comment_file = re.findall(r"\[\[(file|image):(.*?)(\|.*?)?\]\].*?", comment_body)
            find_urls = re.findall(r"\[\[(url):(.*?)\|(.*?)\]\].*?", comment_body)
            if find_urls:
                comment_body = re.sub(r"\[\[(url):(.*?)\]\].*?", f"[{find_urls[0][2]}]({find_urls[0][1]})", comment_body)
            if comment_file and comment_body and comment_body != 'null':
                for comm_file in comment_file:
                    counter = 0
                    for tup in file_links:
                        if tup[0] in comm_file[1]:
                            comment_body = re.sub(rf"\[\[{comm_file[0]}:{comm_file[1]}(\|.*?)?\]\]", f"![{tup[0]}]({tup[1]})", comment_body)
                        elif tup[0][:-4] in comm_file[1] or tup[0][:-5] in comm_file[1]:
                            comment_body = re.sub(rf"\[\[{comm_file[0]}:{comm_file[1]}(\|.*?)?\]\]", f"[{tup[0]}]({tup[1]})", comment_body)
                        else:
                            counter += 1
                    if counter == len(file_links):
                        comment_body = re.sub(rf"\[\[{comm_file[0]}:{comm_file[1]}(\|.*?)?\]\]", f"[file:{comm_file[1]}]", comment_body)
            if comment_body:
                issue.create_comment(body=comment_body)
                print(f"Created comment: {comment_body[:35]}", issue.title)
    if ticket["status"] == "Invalid":
        issue.edit(state='closed', labels=["Invalid"])
        print("Closed issue: invalid")
    if ticket["status"] == "Fixed":
        issue.edit(state='closed', labels=["Fixed"])  # Close fixed
        print("Fixed issue: fixed")

def main():
    global COMMITS

    ap = ArgumentParser()
    ap.add_argument("--delete", required=False, action='store_true')
    ap.add_argument(
        "--download",
        action='store_true',
        help="Download files from Assembla.",
        required=False,
    )
    ap.add_argument(
        "--rename",
        action='store_true',
        help="Rename files in the directory according to their extensions and id.",
        required=False,
    )
    ap.add_argument(
        "--repo", help="GitHub repository in user/repo format", required=False
    )
    ap.add_argument("--update", action='store_true', required=False)
    ap.add_argument("--upload", action='store_true', required=False)
    args = vars(ap.parse_args())
    tickets = None

    folders = [name for name in os.listdir(".") if os.path.isdir(os.path.join(".", name)) and not name.startswith('__')]
    print("List of available directories: ")
    for c, folder in enumerate(folders, start=1):
        print(f"{c}. [{folder}]")
    while True:
        try:
            i = int(input("\nWhich one to use?  "))
            if i < 1 or i > len(folders):
                print("Wrong input. Try again or type Ctrl+C to quit.")
            else:
                break
        except ValueError:
            print("You can only type numbers. Try again or type Ctrl+C to quit.")
            continue
    folder = folders[i-1]
    bak = ''
    files = [f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))]
    for file in files:
        if file.endswith('.bak'):
            bak = file
    if not bak:
        print(".bak file was not found in", folder)
        exit(0)

    os.chdir(folder)
    print(folder)
    if not os.path.isdir("temp"):
        os.mkdir("temp")
    if not os.path.isdir(FILES_DIR):
        os.mkdir(FILES_DIR)

    working_repo = args["repo"]

    if args["delete"]:
        deleteIssues(working_repo)
        print("Done deleting. Exit.")
        exit(0)

    with open(bak, 'r', encoding='utf-8') as file:
        tickets = file.read()

    tickets_arr = parseTickets(tickets)
    # sort tickets by time
    sorted_tickets_array = sorted(
        tickets_arr,
        key=lambda x: datetime.strptime(x['ticket_created_on'], '%Y-%m-%dT%H:%M:%S.000+00:00')
    )
    # find ticket changes
    status_arr = parseStatus(tickets)

    # sort by time
    sorted_status_array = sorted(
        status_arr,
        key=lambda x: datetime.strptime(x['created_at'], '%Y-%m-%dT%H:%M:%S.000+00:00')
    )
    # link statuses to tickets
    sorted_tickets_array, sorted_status_array = linkStatus(sorted_tickets_array, sorted_status_array, tickets)

    # Get file refs from .bak file.
    bak_refs = [
        get_file_id(item) for item in re.findall(
            r"\[\[(?:file|image):(.*?)(?:\|.*?)?\]\]", tickets
        )
    ]

    if args["download"]:
        find_assembla_space = re.search(r'tickets,\s\[\d+,\d+,[\"\d\w\-\_]+,[\"\d\w\-\_]+,\"([\d\w\-\_]+)\"', tickets)
        space_id = find_assembla_space.group(1)
        print("Using assembla space ID: ", find_assembla_space.group(1))
        parseAttachmentsFromBak(space_id, bak_refs)
        print("Done fetching attachments.")
        exit(0)

    if args["rename"]:
        renameFiles(sorted_tickets_array)
        print("Done renaming.")
        exit(0)

    if args["upload"]:
        # Get files for upload to GitHub and Syrinscape.
        files_for_github = []
        files_for_syrinscape = []
        for file in os.listdir(FILES_DIR):
            if os.path.isfile(os.path.join(FILES_DIR, file)):
                size = os.path.getsize(os.path.join(FILES_DIR, file))
                to_github = False
                for ext, size_limit in EXTS.items():
                    if file.lower().endswith(ext.lower()):
                        if size < size_limit:
                            to_github = True
                        break
                if to_github:
                    files_for_github.append(file)
                else:
                    files_for_syrinscape.append(file)
        # Upload files to GitHub.
        if os.path.isfile('files.txt') and os.path.getsize('files.txt'):
            i = input("files.txt exists and is not empty. If you are going to use new github repo, remove it. Remove? YES/NO\n")
            if i == 'YES' or i == 'Y' or i == 'y' or i == 'yes':
                os.remove(f"files.txt")
        ready_files = uploadToGithub(files_for_github, bak_refs, working_repo)
        # Upload files to Syrinscape.
        ready_files += uploadToSyrinscape(files_for_syrinscape, bak_refs)
        exit(0)
    elif os.path.isfile('files.txt'):
        with open('files.txt') as files_txt:
            ready_files = files_txt.read()
    else:
        ready_files = ""

    # Get repo.
    github_check_rate_limit()
    repo = g.get_repo(working_repo)

    # Get commits one time only.
    github_check_rate_limit()
    COMMITS = github_iter(repo.get_commits())

    print("Using repo: ", repo)

    if args["update"]:
        print("Updating existing tickets...")
        issues = github_iter(repo.get_issues(state='all'))
        with open('files.txt', 'r') as file:
            ready_files = file.read()
        ready_urls = re.findall(r".*?\[(.*?)\]\((.*?)\).*?", ready_files)
        ready_links = re.findall(r".*?\!\[(.*?)\]\((.*?)\).*?", ready_files)
        get_img = re.findall(r"alt=\"(.*?)\"\ssrc=\"(.*?)\"", ready_files)
        ready_links.extend(ready_urls)
        ready_links.extend(get_img)
        for issue in issues:
            isbody = issue.body
            file_urls = re.findall(r".*?\[(.*?)\]\((.*?)\).*?", isbody)
            file_links = re.findall(r".*?\!\[(.*?)\]\((.*?)\).*?", isbody)
            file_links.extend(file_urls)
            failed_files = re.findall(r".*?\[file:(.*?)\].*?", isbody)
            maybe_required = (len(failed_files) + len(file_links)) * len(ready_links)
            github_check_rate_limit(core=maybe_required)
            if failed_files:
                for link in failed_files:
                    for fi in ready_links:
                        if link in fi[0]:
                            isbody = re.sub(rf".*?\[file:(.*?)\].*?", f"![{fi[0]}]({fi[1]})", isbody)
                            issue.edit(body=isbody)
                            print(f"Updating [{issue.title}]")
            if file_links:
                for link in file_links:
                    for fi in ready_links:
                        if link[0] in fi[0]:
                            isbody = re.sub(rf".*?\!\[({link[0]})\]\(({link[1]})\).*?", f"![{fi[0]}]({fi[1]})", isbody)
                            isbody = re.sub(rf".*?\[({link[0]})\]\(({link[1]})\).*?", f"![{fi[0]}]({fi[1]})", isbody)
                            issue.edit(body=isbody)
                            print(f"Updating [{issue.title}]")
            comments = github_iter(issue.get_comments())
            for comment in comments:
                combody = comment.body
                file_urls = re.findall(r".*?\[(.*?)\]\((.*?)\).*?", combody)
                file_links = re.findall(r".*?\!\[(.*?)\]\((.*?)\).*?", combody)
                file_links.extend(file_urls)
                failed_files = re.findall(r".*?\[file:(.*?)\].*?", combody)
                maybe_required = (len(failed_files) + len(file_links)) * len(ready_links)
                github_check_rate_limit(core=maybe_required)
                if failed_files:
                    for link in failed_files:
                        for fi in ready_links:
                            if link in fi[0]:
                                combody = re.sub(rf".*?\[file:(.*?)\].*?", f"![{fi[0]}]({fi[1]})", combody)
                                comment.edit(body=combody)
                                print(f"Updating comment in [{issue.title}]")
                if file_links:
                    for link in file_links:
                        for fi in ready_links:
                            if link[0] in fi[0]:
                                combody = re.sub(rf".*?\!\[({link[0]})\]\(({link[1]})\).*?", f"![{fi[0]}]({fi[1]})", combody)
                                combody = re.sub(rf".*?\[({link[0]})\]\(({link[1]})\).*?", f"![{fi[0]}]({fi[1]})", combody)
                                comment.edit(body=combody)
                                print(f"Updating comment in [{issue.title}]")
        print("Done updating. Exit.")
        exit(0)


    file_links = []
    if ready_files:
        # whole list, looks for pictures
        file_links = re.findall(r".*?\!\[(.*?)\]\((.*?)\).*?", str(ready_files))
        # extended list, looks for files
        file_urls = re.findall(r".*?\[(.*?)\]\((.*?)\).*?", str(ready_files))
        # extended list, looks for images with html formatting
        get_img = re.findall(r"alt=\"(.*?)\"\ssrc=\"(.*?)\"", str(ready_files))
        file_links.extend(file_urls)
        file_links.extend(get_img)

    # add usernames to tickets according to user ids
    for ticket in sorted_tickets_array:
        for key in val.keys():
            if ticket["ticket_reporter_id"] == val[key]:
                ticket["ticket_username"] = key
        for comment in ticket["ticket_comments"]:
            for key in val.keys():
                if comment["user_id"] == val[key]:
                    comment["username"] = key

    # Get issues one time only.
    issues = github_iter(repo.get_issues(state='all'))
    for ticket in sorted_tickets_array:
        issue_name = f"""{ticket["ticket_title"]}"""
        lock = None
        for check_issue in issues:
            if check_issue and check_issue.title == issue_name:
                print("Issue exists; passing: [", check_issue.title, "]")
                lock = 1
        if not lock:
            while True:
                if len(issues) == int(ticket["ticket_number"]-1) or \
                    len(issues) > int(ticket["ticket_number"]-1):
                    break
                else:
                    print(f"Creating dummy issue until {ticket['ticket_number']}: current counter is {len(issues)+1}")
                    iss = repo.create_issue(title="null", body="null")
                    issues.append(iss)
                    iss.edit(state='closed')
                    continue
            issue = createIssue(issue_name, ticket, repo, file_links)
            issues.append(issue)
            addComments(ticket, issue, file_links, repo)

if __name__ == "__main__":
    with ipdb.launch_ipdb_on_exception():
        main()
