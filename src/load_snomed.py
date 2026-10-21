#!/usr/bin/env python3
"""Load SNOMED CT RF2 Snapshot concept labels into Microsoft SQL Server."""
import argparse
import csv
import math
import os
from pathlib import Path
import sys
import time

FSN = '900000000000003001'
SYNONYM = '900000000000013009'
PREFERRED = '900000000000548007'
DEFAULT_LANGUAGE_REFSETS = ['999001261000000100', '999000691000001104']


def configure_csv_field_limit():
    """Allow very large fields seen in some release text rows."""
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def files(root, pattern):
    pattern_lc = pattern.lower()
    matches = []
    for base, _, names in os.walk(root, followlinks=True):
        base_path = Path(base)
        parts_lc = {part.lower() for part in base_path.parts}
        if 'snapshot' not in parts_lc:
            continue
        for name in names:
            if not name.lower().endswith('.txt'):
                continue
            if not name.lower().startswith(pattern_lc):
                continue
            matches.append(base_path / name)
    return sorted(matches)
     

def rows(path):
    with path.open('r', encoding='utf-8-sig', newline='') as stream:
        # RF2 files are tab-delimited plain text; disable quote handling to
        # avoid mis-parsing terms that contain quote characters.
        yield from csv.DictReader(stream, delimiter='\t', quoting=csv.QUOTE_NONE)


def latest_by_id(paths, label):
    """Merge overlapping RF2 Snapshot modules by component ID and effectiveTime."""
    print(f'[{label}] reading {len(paths)} file(s)...', flush=True)
    started = time.time()
    result = {}
    for idx, path in enumerate(paths, start=1):
        print(f'[{label}] file {idx}/{len(paths)}: {path.name}', flush=True)
        for row in rows(path):
            old = result.get(row['id'])
            if old is None or row['effectiveTime'] > old['effectiveTime']:
                result[row['id']] = row
            elif row['effectiveTime'] == old['effectiveTime'] and row != old:
                raise ValueError(f"Conflicting versions for component {row['id']}")
    elapsed = time.time() - started
    print(f'[{label}] complete: {len(result):,} latest rows in {elapsed:.1f}s', flush=True)
    return result


def main():
    configure_csv_field_limit()
    started = time.time()
    print('Starting SNOMED load...', flush=True)
    parser = argparse.ArgumentParser()
    parser.add_argument('release', type=Path, help='Extracted Monolith root directory')
    parser.add_argument('--connection', default=os.getenv('SNOMED_SQL_CONNECTION'),
                        help='ODBC connection string (or SNOMED_SQL_CONNECTION)')
    parser.add_argument('--output-file', type=Path,
                        help='Write final concept rows to this tab-delimited file')
    parser.add_argument('--batch-size', type=int, default=5000)
    args = parser.parse_args()
    if not args.release.exists():
        parser.error(f'Release path does not exist: {args.release}')
    if not args.release.is_dir():
        parser.error(f'Release path is not a directory: {args.release}')
    write_sql = bool(args.connection)
    write_file = args.output_file is not None
    if not write_sql and not write_file:
        parser.error('Provide --connection/SNOMED_SQL_CONNECTION and/or --output-file')
    language_refsets = set(DEFAULT_LANGUAGE_REFSETS)
    print(f'Language refsets: {", ".join(sorted(language_refsets))}', flush=True)

    print(f'Searching for Snapshot text files under {args.release}...', flush=True)
    concept_paths = files(args.release, 'sct2_Concept_MONOSnapshot')
    description_paths = files(args.release, 'sct2_Description_MONOSnapshot')
    language_paths = files(args.release, 'der2_cRefset_LanguageMONOSnapshot')
    for label, paths in [('concept', concept_paths), ('description', description_paths), ('language', language_paths)]:
        if not paths:
            parser.error(f'No {label} Snapshot files found beneath {args.release}')
        print(f'{label}: {len(paths)} files', flush=True)

    print('Indexing latest component versions...', flush=True)
    concepts = latest_by_id(concept_paths, 'concept')
    descriptions = latest_by_id(description_paths, 'description')
    language = latest_by_id(language_paths, 'language')
    print(f'Components: {len(concepts):,} concepts; {len(descriptions):,} descriptions; {len(language):,} language members', flush=True)
    snomed_version = max((concept['effectiveTime'] for concept in concepts.values()), default='')
    print(f'SNOMED version (max effectiveTime): {snomed_version}', flush=True)

    print('Resolving preferred language members...', flush=True)
    preferred_description_ids = set()
    for member in language.values():
        if (member['active'] == '1' and member['refsetId'] in language_refsets
                and member['acceptabilityId'] == PREFERRED):
            preferred_description_ids.add(member['referencedComponentId'])
    print(f'Preferred description links: {len(preferred_description_ids):,}', flush=True)

    print('Building FSN/PT lookup maps...', flush=True)
    fsns = {}
    preferred = {}
    for desc in descriptions.values():
        if desc['active'] != '1':
            continue
        concept_id = desc['conceptId']
        if desc['typeId'] == FSN:
            fsns.setdefault(concept_id, []).append(desc)
        elif desc['typeId'] == SYNONYM and desc['id'] in preferred_description_ids:
            preferred.setdefault(concept_id, []).append(desc)

    def choose(items):
        if not items:
            return None
        # Prefer newest description; deterministic if a release has ambiguous choices.
        return sorted(items, key=lambda x: (x['effectiveTime'], x['id']), reverse=True)[0]['term']

    conn = None
    cursor = None
    if write_sql:
        import pyodbc
        print('Connecting to SQL Server...', flush=True)
        conn = pyodbc.connect(args.connection, autocommit=False)
        cursor = conn.cursor()
        print('Creating staging table dbo.SnomedConcept_stage...', flush=True)
        cursor.execute('''
        IF OBJECT_ID(N'dbo.SnomedConcept_stage', N'U') IS NOT NULL
            DROP TABLE dbo.SnomedConcept_stage;
        CREATE TABLE dbo.SnomedConcept_stage (
            ConceptId BIGINT NOT NULL PRIMARY KEY,
            Active BIT NOT NULL,
            PreferredTerm NVARCHAR(2048) NULL,
            FSN NVARCHAR(2048) NULL
        );''')
        conn.commit()
        cursor.fast_executemany = True

    file_stream = None
    file_writer = None
    if write_file:
        print(f'Writing tab-delimited output to {args.output_file}...', flush=True)
        file_stream = args.output_file.open('w', encoding='utf-8', newline='')
        file_writer = csv.writer(file_stream, delimiter='\t', lineterminator='\n')
        file_writer.writerow(['ConceptId', 'Active', 'PreferredTerm', 'FSN'])

    batch = []
    missing_pt = missing_fsn = 0
    total_concepts = len(concepts)
    total_batches = max(1, math.ceil(total_concepts / args.batch_size))
    written = 0
    batch_no = 0
    if write_sql:
        print(f'Loading staging table in {total_batches} batch(es) of up to {args.batch_size:,}...', flush=True)
    else:
        print(f'Building {total_concepts:,} final rows for tab-delimited output...', flush=True)
    for concept_id, concept in concepts.items():
        pt = choose(preferred.get(concept_id))
        fsn = choose(fsns.get(concept_id))
        if pt is None:
            missing_pt += 1
        if fsn is None:
            missing_fsn += 1
        row = (int(concept_id), int(concept['active']), pt, fsn)
        if file_writer is not None:
            file_writer.writerow(row)
        if write_sql:
            batch.append(row)
            if len(batch) >= args.batch_size:
                cursor.executemany('INSERT INTO dbo.SnomedConcept_stage (ConceptId, Active, PreferredTerm, FSN) VALUES (?, ?, ?, ?)', batch)
                conn.commit()
                batch_no += 1
                written += len(batch)
                pct = (written / total_concepts) * 100 if total_concepts else 100.0
                print(f'Batch {batch_no}/{total_batches} committed ({written:,}/{total_concepts:,}, {pct:.1f}%)', flush=True)
                batch.clear()

    if file_stream is not None:
        file_stream.close()

    if write_sql:
        if batch:
            cursor.executemany('INSERT INTO dbo.SnomedConcept_stage (ConceptId, Active, PreferredTerm, FSN) VALUES (?, ?, ?, ?)', batch)
            conn.commit()
            batch_no += 1
            written += len(batch)
            pct = (written / total_concepts) * 100 if total_concepts else 100.0
            print(f'Batch {batch_no}/{total_batches} committed ({written:,}/{total_concepts:,}, {pct:.1f}%)', flush=True)

        print('Validating row count...', flush=True)
        cursor.execute('SELECT COUNT_BIG(*) FROM dbo.SnomedConcept_stage')
        count = cursor.fetchone()[0]
        if count != len(concepts):
            raise RuntimeError(f'Row count mismatch: expected {len(concepts)}, got {count}')

        # Swap only after successful load. Keep old table if present until stage validated.
        print('Swapping staging table into dbo.SnomedConcept...', flush=True)
        cursor.execute('''
        BEGIN TRY
          BEGIN TRANSACTION;
          IF OBJECT_ID(N'dbo.SnomedConcept', N'U') IS NOT NULL
            DROP TABLE dbo.SnomedConcept;
          EXEC sp_rename N'dbo.SnomedConcept_stage', N'SnomedConcept';
          COMMIT TRANSACTION;
        END TRY
        BEGIN CATCH
          IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
          THROW;
        END CATCH;''')
        conn.commit()

        print('Updating dbo.SnomedVersion metadata table...', flush=True)
        cursor.execute('''
        IF OBJECT_ID(N'dbo.SnomedVersion', N'U') IS NULL
        BEGIN
            CREATE TABLE dbo.SnomedVersion (
                SnomedVersion CHAR(8) NOT NULL,
                SourceReleasePath NVARCHAR(2048) NOT NULL,
                LoadedAtUtc DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME()
            );
        END;
        TRUNCATE TABLE dbo.SnomedVersion;
        INSERT INTO dbo.SnomedVersion (SnomedVersion, SourceReleasePath)
        VALUES (?, ?);''', snomed_version, str(args.release))
        conn.commit()
        conn.close()
    else:
        count = len(concepts)

    total_elapsed = time.time() - started
    if write_sql and write_file:
        destination = f'SQL Server and {args.output_file}'
    elif write_sql:
        destination = 'SQL Server'
    else:
        destination = str(args.output_file)
    print(f'Loaded {count:,} concepts to {destination}; missing preferred term: {missing_pt:,}; missing active FSN: {missing_fsn:,}')
    print(f'Total elapsed time: {total_elapsed:.1f}s')
    print('Note: inactive concepts may legitimately lack active descriptions.')


if __name__ == '__main__':
    main()
