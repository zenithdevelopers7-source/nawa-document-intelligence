"""Read-only recovery inventory for the offline synthetic prototype."""
import re

_ID = re.compile(r'^[0-9a-f]{32}$')


def inspect_storage(store, jobs):
    """Report discrepancies; run only when intake and workers are stopped.

    This reports, but never deletes or repairs files. Tenant authorization,
    live-operation consistency and production recovery are not implemented.
    """
    references = dict(jobs.recovery_snapshot())
    invalid = sorted(document_id for document_id in references if not _ID.fullmatch(document_id))
    valid = {key: value for key, value in references.items() if _ID.fullmatch(key)}
    files = {p.name: p for p in store.directory.iterdir()}
    missing, corrupt, unsafe = [], [], []
    for document_id, digest in valid.items():
        path = files.get(document_id)
        if path is None:
            missing.append(document_id)
        elif path.is_symlink() or not path.is_file():
            unsafe.append(document_id)
        else:
            try:
                store.read_verified(document_id, digest)
            except (OSError, ValueError):
                corrupt.append(document_id)
    # Also report unreferenced entries with valid-looking IDs when they are
    # directories, symlinks or other non-regular files. They are not safe
    # orphaned files and must not disappear from the inventory.
    unsafe.extend(name for name, path in files.items()
                  if _ID.fullmatch(name) and name not in references
                  and (path.is_symlink() or not path.is_file()))
    return {
        'orphaned_files': sorted(name for name, path in files.items()
                                 if _ID.fullmatch(name) and name not in references
                                 and path.is_file() and not path.is_symlink()),
        'missing_files': sorted(missing),
        'corrupt_files': sorted(corrupt),
        'unsafe_entries': sorted(unsafe),
        'invalid_references': invalid,
        'unrecognized_entries': sorted(name for name in files if not _ID.fullmatch(name)),
    }
