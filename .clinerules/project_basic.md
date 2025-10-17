# Project Basic

This project is to build a wikipedia dump processing pipeline in addition to a wikipedia data interactive web app.

This is a Django project.

All the wikipedia information should be from the HotpotQA project. [HotPot QA Wikipedia](https://hotpotqa.github.io/wiki-readme.html)

The wikipedia is a 2017 dump with non-standard structure.

The project should start with SQLite database, located in data/db.sqlite3

## Project Objectives

### Data pipeline

#### Input

The raw data is a 2017 wikipedia dump with non-standard structure.

The raw data is a compressed file named "enwiki-20171001-pages-meta-current-withlinks-processed.tar.bz2"

When decompressed, it has the following file structure. The decompressed folder should be put into data/processed/

```tree
enwiki-20171001-pages-meta-current-withlinks/
├── AA
│   ├── wiki_00.bz2
│   ├── wiki_01.bz2
...
│   └── wiki_99.bz2
├── AB
...
└── FZ
    ├── wiki_00.bz2
    ├── wiki_01.bz2
    ├── wiki_02.bz2
    ├── wiki_03.bz2
    ├── wiki_04.bz2
    ├── wiki_05.bz2
    ├── wiki_06.bz2
    ├── wiki_07.bz2
    ├── wiki_08.bz2
    ├── wiki_09.bz2
    ├── wiki_10.bz2
    ├── wiki_11.bz2
    ├── wiki_12.bz2
    ├── wiki_13.bz2
    ├── wiki_14.bz2
    ├── wiki_15.bz2
    └── wiki_16.bz2

157 directories, 15517 files
```

When a wiki_00.bz2 is extracted, part of it is .clinerules/example_wikipedia_dump-small.md.

InternalLink:

- <a href=\"Pierre-Joseph%20Proudhon\">Pierre-Joseph Proudhon</a>
- <a href=\"individualist%20anarchism\">individualist anarchism</a>

InternalLink in a paragraph:

- Various factions within the <a href=\"French%20Revolution\">French Revolution</a> labelled opponents as anarchists (as <a href=\"Maximilien%20de%20Robespierre\">Robespierre</a> did the <a href=\"H%C3%A9bertists\">H\u00e9bertists</a>) although few shared many views of later anarchists.
- the anarcho-syndicalist trade union <a href=\"Unione%20Sindacale%20Italiana\">Unione Sindacale Italiana</a> \"grew to 800,000 members and the influence of the Italian Anarchist Union (20,000 members plus \"<a href=\"Umanita%20Nova\">Umanita Nova</a>\", its daily paper) grew accordingly\u00a0... Anarchists were the first to suggest occupying workplaces.

#### Processing

All the wikipedia data should be loaded into the database.

### Data visualization

This is a web application. It is a search engine where user can search for wikipedia articles using keywords.

It is made up of two pages. The first is the search/results page, which includes a search bar and a list of results. The result should only have a title.

The second page is the article page. It includes all the content of the wikipedia article we extracted from the dump.

For this web app, we should use as little java script as possible.
