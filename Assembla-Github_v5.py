import calendar
import time

from github import Github
from github.GithubException import RateLimitExceededException
import ast, os, re, sys, glob, io, requests, zipfile
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

g = Github(Credentials.github_user, Credentials.github_password)


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
                "GitHub API rate limit nearly depleted. Sleeping for %s seconds. %r" % (
                    sleep_time, limit
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

val = {
# add here your user and space_id of each contributor separated with a comma - for example
    "securedglobe": "c68pgUDuer4PiDacwqjQWU",
}

EXTS = ['jpg', 'png', 'jpeg', 'docx', 'log', 'pdf', 'pptx', 'txt', 'zip', 'JPG', 'PNG']
# list of allowed extensions

def every_downloads_chrome(driver):
    # waits for download to complete
    if not driver.current_url.startswith("chrome://downloads"):
        driver.get("chrome://downloads/")
    return driver.execute_script("""
        var items = downloads.Manager.get().items_;
        if (items.every(e => e.state === "COMPLETE"))
            return items.map(e => e.fileUrl || e.file_url);
        """)

def parseAttachmentsFromBak(sid, tickets):
    filelist = []
    link = f"https://bigfiles.assembla.com/spaces/{sid}/documents/download/"
    # get all attachments from .bak file
    # save them in a separate list
    if not os.path.isfile('filenames.txt'):
        find_files = re.findall(r".*?\[\[(file|image):(.*?)(\|.*?)?\]\].*?", tickets)
        for file in find_files:
            if file:
                filelist.append(rf"{file[1]}")
    else:
        dirfile = glob.glob(f"{FILES_DIR}\**")
        with open("filenames.txt") as file:
            for line in file:
                filelist.append(line.strip())
        for file in dirfile:
            file = file.replace(f"{FILES_DIR}\\", "")
            file = file[:file.rfind(".")]
            for c, fi in enumerate(filelist):
                if fi in file:
                    del filelist[c]
                elif os.path.isfile(FILES_DIR+'\\'+fi):
                    del filelist[c]
    chrome_options = webdriver.ChromeOptions()
    path = os.path.dirname(os.path.realpath(__file__))
    chrome_options.add_experimental_option("prefs", {
    "download.default_directory": f"{path}\\temp",
    "download.prompt_for_download": False,
    "download.directory_upgrade": True,
    "safebrowsing.enabled": True
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

    for file in filelist:
        # fetch all files from the filelist
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
        sleep(8)
        temps = glob.glob(f"temp\**")
        try:
            for tm in temps:
                if tm.endswith('jpg'):
                    os.rename(tm, f"files\{file}.jpg")
                elif tm.endswith('png'):
                    os.rename(tm, f"files\{file}.png")
                elif tm.endswith('zip'):
                    os.rename(tm, f"files\{file}.zip")
                elif tm.endswith('pdf'):
                    os.rename(tm, f"files\{file}.pdf")
                elif tm.endswith('docx'):
                    os.rename(tm, f"files\{file}.docx")
                elif tm.endswith('txt'):
                    os.rename(tm, f"files\{file}.txt")
                else:
                    os.rename(tm, f"files\{file}")
        except FileExistsError:
            pass
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
            "ticket_title": array[5],
            "ticket_priority": array[6],
            "ticket_description": array[7],
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
                "comment": array[5],
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
                    fname = attach["filename"]
                    fid = attach["file_id"]
                    if not fname.endswith('.png') and not fname.endswith('.jpg') \
                        and not fname.endswith('.PNG') and not fname.endswith('.JPG'):
                        dot = re.search(r"\..*", fname)
                        dot = "" if not dot else dot.group(0)
                        try:
                            get_file = glob.glob(f"{FILES_DIR}\{fid}.*")
                            if not get_file:
                                get_file = glob.glob(f"{FILES_DIR}\{fid}")
                            get_dot = re.search(r"\..*", get_file[0])
                            get_dot = "" if not get_dot else get_dot.group(0)
                            if get_dot and not dot:
                                dot = get_dot
                            if get_dot.endswith('.png') or get_dot.endswith('.jpg') or get_dot.endswith('.PNG') \
                                or get_dot.endswith('.JPG'):
                                pass
                            else:
                                if os.path.isfile(f"{FILES_DIR}\{fid}{dot}"):
                                    pass
                                else:
                                    print(f"Renaming: {fid} -> {fid}{dot}")
                                    os.rename(get_file[0], f"{FILES_DIR}\{fid}{dot}")
                                counter = 0
                                for ext in EXTS:
                                    if ext not in dot:
                                        counter += 1
                                    else:
                                        pass
                                if counter == len(EXTS) and not get_file[0].endswith(".htm"):
                                    # not attachable
                                    print(f"Making zip file -> {fid}.zip")
                                    if os.path.isfile(f"{FILES_DIR}\{fid}.zip"):
                                        os.remove(f"{FILES_DIR}\{fid}.zip")
                                    obj = zipfile.ZipFile(f"{FILES_DIR}\{fid}.zip", 'w')
                                    obj.write(f"{FILES_DIR}\{fid}{dot}")
                                    obj.close()
                        except Exception:
                            pass # doesn't exist


def uploadToGithub(dirfiles, tickets, working_repo):
    filelist = []
    ready_files = ""
    path = os.path.dirname(os.path.realpath(__file__))
    # filter attachments from .bak file to remove attachments not allowed or not existing
    find_files = re.findall(r".*?\[\[(file|image):(.*?)(\|.*?)?\]\].*?", tickets)

    for file in find_files:
        for dr in dirfiles:
            di = str(dr.replace(f"{FILES_DIR}\\", ""))
            di = di[:di.rfind('.')]
            if di in file[1]:
                filelist.append(f"{path}\{FILES_DIR}\{dr}")
    if os.path.isfile('files.txt'):
        print('files.txt exists, parsing existing links...')
        ex_files = ""
        # check for existing links and remove duplicates
        with open('files.txt', 'r') as file:
            ex_files = file.read()
        file_links = re.findall(r".*?\!\[(.*?)\]\((.*?)\).*?", ex_files)
        file_urls = re.findall(r".*?\[(.*?)\]\((.*?)\).*?", ex_files)
        get_img = re.findall(r"alt=\"(.*?)\"\ssrc=\"(.*?)\"", ex_files)
        file_links.extend(get_img)
        file_links.extend(file_urls)
        for flink in file_links:
            for co, fi in enumerate(filelist):
                if flink[0] in fi:
                    del filelist[co]
    if not filelist:
        print("uploadToGithub: Nothing to upload.")
        return 1
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
    # split filelist into chunks of 8 files
    chunks = [filelist[i:i + 2] for i in range(0, len(filelist), 2)]
    for chunk in chunks:
        chk = (' \n ').join(chunk)
        findBody = driver.find_element_by_id("issue_body")
        findBody.clear()
        findButton = driver.find_element_by_id("fc-issue_body")
        findButton.clear()
        if chk:
            findButton.send_keys(chk)
        print("Waiting for uploads to finish...")
        sleep(5)
        while True:
            chk = findBody.get_attribute('value')
            # [Uploading czo0qWjmmr5PZcdmr6CpXy.zip…]()
            if "]()" in chk:
                sleep(5)
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
    ap.add_argument("-r", "--repo", required=True, help="Working repository in format user/repo")
    ap.add_argument("-re", "--rename", required=False, action='store_true',
    help="Rename files in the directory according to their extensions and unique ids; pack them to zip.")
    ap.add_argument("-d", "--download", required=False, action='store_true',
    help="Download files from Assembla space. Rename files in the directory according to their extensions and unique ids; pack them to zip.")
    ap.add_argument("-u", "--update", required=False, action='store_true')
    ap.add_argument("-del", "--delete", required=False, action='store_true')
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

    if args["download"]:
        find_assembla_space = re.search(r'tickets,\s\[\d+,\d+,[\"\d\w\-\_]+,[\"\d\w\-\_]+,\"([\d\w\-\_]+)\"', tickets)
        assembla_id = find_assembla_space.group(1)
        print("Using assembla space ID: ", find_assembla_space.group(1))
        parseAttachmentsFromBak(assembla_id, tickets)
        print("Done fetching attachments.")
        renameFiles(sorted_tickets_array)
        print("Done renaming.")

    if args["rename"]:
        renameFiles(sorted_tickets_array)
        print("Done renaming.")
        exit(0)

    github_check_rate_limit()
    repo = g.get_repo(working_repo)

    # Get commits one time only.
    COMMITS = github_iter(repo.get_commits())

    print("Using repo: ", repo)

    # get list of available files for transfer
    dirfiles = []
    # filter attachments by allowed extensions in github issues
    dirfile = []
    for (_, _, filenames) in os.walk(f"{FILES_DIR}"):
        dirfile.extend(filenames)
        break
    for dr in dirfile:
        for ext in EXTS:
            if dr.endswith(ext):
                dirfiles.append(dr)

    ready_files = ""

    if not os.path.isfile('files.txt'):
        ready_files = uploadToGithub(dirfiles, tickets, working_repo)
        if ready_files == 1:
            print("No files to parse.")
        pass
    else:
        with open('files.txt', 'r') as file:
            ready_files = file.read()
        if len(ready_files) != 0:
            while True:
                i = input("files.txt exists and is not empty. If you are going to use new github repo, remove it. Remove? YES/NO\n")
                if i == 'YES' or i == 'Y' or i == 'y' or i == 'yes':
                    os.remove(f"files.txt")
                    ready_files = uploadToGithub(dirfiles, tickets, working_repo)
                    if ready_files == 1:
                        print("No files to parse.")
                    break
                else:
                    ready_files = uploadToGithub(dirfiles, tickets, working_repo)
                    if ready_files == 1:
                        print("No new files to parse.")
                    break
        else:
            ready_files = uploadToGithub(dirfiles, tickets, working_repo)
            if ready_files == 1:
                print("No files to parse.")

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
        try:
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
        except RateLimitExceededException as e:
            # wait 1 hour for rate limit
            print(e, "Waiting 1 hour...")
            sleep(60*61)
            continue
        except Exception as e:
            print(e)
            pass

if __name__ == "__main__":
    main()