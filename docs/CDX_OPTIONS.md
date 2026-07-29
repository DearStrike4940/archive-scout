# CDX options

Archive Scout builds resumable queries for the Wayback Machine CDX endpoint. The app fixes the response format and fields it needs, while allowing normal research filters and collapse behavior.

## Targets

Enter one target per line:

```text
example.com/*
forum.example.com/*
example.com/forum/*
example.com/showthread.php?*
```

A line without `*` is normalized into a path-prefix pattern.

## Date range

Accepted forms:

```text
2001
200109
20010911
20010911083000
```

A short start value expands to the beginning of the period. A short end value expands to the end of the period.

Examples:

```text
Start: 20010901
End:   20010930
```

```text
Start: 20010911000000
End:   20010911235959
```

The app splits a multi-year request into yearly windows and clamps the first and last windows to the exact selected range.

## Filters

Enter filter values without the `filter=` prefix, one per line:

```text
statuscode:200
mimetype:text/html
```

The default preset uses:

```text
statuscode:200
```

Multiple filter lines are sent as repeated CDX `filter` parameters.

## Collapse

The interface offers two independent options:

```text
collapse=urlkey
collapse=digest
```

`urlkey` reduces repeated captures of the same normalized URL. `digest` reduces captures with identical archived content. The local database still keeps the earliest timestamp observed for every original URL.

Turning off every collapse option can return far more rows and take substantially longer.

## matchType

Available choices:

```text
Automatic
exact
prefix
host
domain
```

Automatic lets the target pattern determine normal CDX behavior. Use `domain` when you want a domain-wide match that includes subdomains. Use `prefix` for a precise URL-path prefix.

## Page size

The page-size setting becomes the CDX `limit` value for each resumable page. The app accepts values from 100 through 10,000.

Larger pages reduce request overhead but take longer to retry after a network failure. The default of 5,000 is a reasonable balance.

## Additional parameters

Enter one decoded `key=value` pair per line:

```text
resolveRevisits=true
fastLatest=true
```

The application validates parameter names and appends the pairs in order. Do not URL-encode values manually; the app performs URL encoding.

The following parameters are reserved because changing them would break parsing, pagination, or the dedicated interface controls:

```text
url
from
to
output
fl
showResumeKey
resumeKey
limit
matchType
```

Use the Filters section for repeated filters and the collapse checkboxes for common collapse parameters.

## Request preview

Select Preview CDX request to display the exact URL for the first target and first year window. This is useful for verifying parameters before starting a large job.

## Query signatures and resume state

Archive Scout hashes every query-affecting option into a query signature. CDX resume keys and current reports are associated with that signature.

Changing any of these creates a new signature:

- date range,
- filters,
- collapse values,
- `matchType`,
- additional parameters,
- or CDX page size.

This prevents a changed query from incorrectly resuming an older one or mixing old indexed rows into the current report.

## Examples

### Earliest successful HTML pages for a forum path

```text
Target:
forum.example.com/archive/*

Start:
2001

End:
2008

Filters:
statuscode:200
mimetype:text/html

Collapse:
urlkey enabled
```

### Domain and subdomains without URL-key collapse

```text
Target:
example.com

matchType:
domain

Collapse:
urlkey disabled
digest disabled
```

This can produce a very large result set.

### Fast URL triage

Use `collapse=urlkey`, keep `statuscode:200`, and select the download scope Only URLs containing a keyword. The app will index the complete URL set but download only text captures whose URL already includes one of the keyword patterns.
