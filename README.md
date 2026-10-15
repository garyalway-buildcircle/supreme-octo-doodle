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
