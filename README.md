# supreme-octo-doodle

Python tools that pull data out of source systems into markdown. Run them from the Makefile.

## Jira

Writes one markdown file per Jira Cloud issue: title, description, URL, status, assignee, issue links, every comment, and the images embedded in the description and comments.

Create an API token at <https://id.atlassian.com/manage-profile/security/api-tokens>, then:

```bash
cp .env.example .env
```

Fill in `JIRA_BASE_URL` (the site root, such as `https://your-site.atlassian.net`), `JIRA_EMAIL`, and `JIRA_API_TOKEN`. Values are unquoted. A `$` in a token must be written as `$$` because Make reads `.env`.

From the repo root:

```bash
make jira PROJECT=PROJ START=100
make jira PROJECT=PROJ START=100 END=120
```

`PROJECT` is the issue key prefix. `START` is the first issue number. Without `END`, the export runs through the highest existing issue in the project. With `END`, that number is the last issue, inclusive, so the second command fetches `PROJ-100` through `PROJ-120`. Each existing ticket is written to `out/jira/PROJ-100.md`. Missing numbers are skipped. Images from the description and comments are saved beside it, for example `out/jira/PROJ-100/screenshot.png`, and the markdown links to those files.

`make` on its own prints the usage lines.

Descriptions and comments are converted from Atlassian document format. The token returns the comments that account is allowed to see. A restricted comment includes its audience in the heading.

## Confluence

Writes one markdown file per current page in a page tree: title, URL, parent, body, footer comments and their replies, inline comments, and the images embedded in the page and comments.

Confluence uses the same `.env` values as Jira. There is no separate token or base URL. If you have not created `.env` yet:

```bash
cp .env.example .env
```

Fill in `JIRA_BASE_URL`, `JIRA_EMAIL`, and `JIRA_API_TOKEN` as described above. The token must be allowed to view the pages.

The page id is the number in the page URL, for example `123456` in `https://your-site.atlassian.net/wiki/spaces/TEAM/pages/123456/Title`.

```bash
make confluence PAGE=123456
```

`PAGE` is the root page. The export includes that page and every current descendant page. Each page is written to `out/confluence/123456.md`. Images are saved beside it, for example `out/confluence/123456/screenshot.png`, and the markdown links to those files. Whiteboards, folders, databases, embeds, and pages that are not current are skipped.

A parent that is also in the export is linked by its page id, for example `[Parent title](123.md)`.

## Links

Rewrites links in the exported markdown so they point at local files when those files exist. A link to another Jira issue becomes `PROJ-102.md` from a Jira file, or `../jira/PROJ-102.md` from a Confluence file. A link to a Confluence page becomes `42.md` or `../confluence/42.md`. The **URL** line on each file stays the remote address. Links to issues or pages that were not exported stay remote, as do links to a different site. Recognized addresses are `/browse/KEY`, `/issues/KEY`, `selectedIssue=KEY`, `/pages/ID`, `/pages/edit-v2/ID`, and `pageId=ID`. A heading fragment on a page link is kept, for example `42.md#Licensing`.

`make jira` and `make confluence` run this after the export succeeds. To connect an existing dump:

```bash
make links
```

## Git

Writes one markdown file per GitHub repository: URL, default branch, commit, commit date, subject, README, languages, top-level layout, project files, and C4 container diagrams for Terraform and AWS CDK. Language counts and the layout skip dependency directories such as `node_modules`, `.terraform`, and `cdk.out`.

A read-only personal access token is enough for a private repository. Public repositories need no token. Create a token with Contents read at <https://github.com/settings/personal-access-tokens>, then add `GIT_TOKEN` to `.env`. A `$` in a token must be written as `$$` because Make reads `.env`.

```bash
make git URL=https://github.com/org/name
make git REPOS=repos.txt
```

`URL` is one repository. `REPOS` is a text file with one repository URL per line. Blank lines and lines starting with `#` are ignored. Pass one of them, not both. `https://github.com/org/name` and `git@github.com:org/name.git` are accepted. Other hosts are not. Each repository is written to `out/git/org-name.md`. The clone is shallow, of the default branch, and is removed after the file is written.

Recognized compute, storage, database, queue, and network resources become containers in a Mermaid C4 diagram. An edge means one of them references another. Argument values and `.tfvars` files are not copied. A supporting resource that points at two of them, such as an event source mapping, becomes an edge and is not itself a container. IAM roles, security groups, and other supporting resources are omitted. A directory of `.tf` files that no other file calls as a module is a Terraform root, and a local module is drawn inside the root that calls it. A remote module is one container. A directory containing `cdk.json` is a CDK app. That diagram reads TypeScript and Python, and skips tests. A construct call counts when it uses a CDK module prefix, such as `s3.Bucket`, or when the class is imported from an aws-cdk package. An edge can cross files when one file imports the construct, or passes a uniquely named property such as `storage.bucket`. A name that appears only inside a string is not an edge.
