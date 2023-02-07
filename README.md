# Assembla to GitHub

See: https://www.codeproject.com/Articles/5247083/Migrating-from-Assembla-to-Github-using-Automation

## How to use

Delete GitHub issues:

```shell
time python Assembla-Github_v5.py --delete --repo syrinscape/syrinscape
```

Download files from Assembla:

```shell
time python Assembla-Github_v5.py --download
```

Rename downloaded files from Assembla:

```shell
time python Assembla-Github_v5.py --rename
```

Upload downloaded files to GitHub:

```shell
time python Assembla-Github_v5.py --upload --repo syrinscape/syrinscape
```

NOTE: GitHub will randomly complain that "Something went really wrong..." Just wait a
while and try again. It appears to be an undocumented rate limit.

Copy the `data/files` directory to a location accessible via `FILES_URL`. Any files that
cannot be uploaded to GitHub will be linked to there.

Create GitHub issues:

```shell
time python Assembla-Github_v5.py --repo syrinscape/syrinscape
```

Update existing GitHub issues (e.g. after a new Assembla export):

```shell
time python Assembla-Github_v5.py --update --repo syrinscape/syrinscape
```
