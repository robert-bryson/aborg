# Open Work

Keep only open tasks in this file.
Complete the acceptance check and update the documentation before you remove a task.
Do not mark a task complete because its test was skipped.

## Priority 1: Data Protection

| ID | Required Result | Acceptance Check |
| --- | --- | --- |
| SAFE-01 | Record each operation in a persistent transaction journal before a file change. Recover interrupted organize and undo operations. | Stop the process before and after each file change and log replacement. Restart it. Verify that all original data can be recovered. |
| SAFE-02 | Lock the collection and undo log during organize, cleanup, rename, corrections, and undo. | Start two writer processes. Verify that the second process makes no changes. Test recovery from a stale lock. |
| SAFE-03 | Limit archive member count, total extracted bytes, and decompression ratio. Check before extraction and during extraction. | Use compressed test archives that exceed each limit. Verify that extraction stops, removes its own partial output, and keeps the source. |
| SAFE-04 | Verify stored content before destructive undo. Refuse changed data or a replacement source archive. | Change a copied file or replace an archive after organize. Verify that undo keeps the changed data and its failed log entry. |
| SAFE-05 | Limit metadata field lengths and bound parser execution time. Check regex backtracking warnings. | Use long malformed tags and configured patterns. Enforce a measured time limit. Keep all normal parsing tests passing. |

## Priority 2: Verification and Performance

| ID | Required Result | Acceptance Check |
| --- | --- | --- |
| TEST-01 | Add Windows CI for file operations and configuration. | Test reserved names, links, reparse points, collisions, and undo on a Windows runner. Report skipped link tests. |
| SCAN-01 | Avoid repeated recursive reads of series directories. Measure cache cost on a large collection and a network share. | Compare time, directory reads, and tag reads with and without the cache. Verify unchanged book results and correct cache invalidation. |
| CLEAN-01 | Show progress during source content verification. | Show checked bytes and total bytes in an interactive terminal. Keep redirected output free of progress animation. |
| CODE-01 | Split complex scan and operation controllers into small, tested functions. Reduce duplicated metadata defaults. | Keep command results unchanged. Add branch tests before extraction. Resolve the related static-analysis findings without rule suppression. |

## Priority 3: Metadata Review

| ID | Required Result | Acceptance Check |
| --- | --- | --- |
| META-01 | Ask the user to review uncertain author and title matches. Show the metadata source and destination. | Test ambiguous two-part names and long-title conflicts. Keep the source until the user accepts the metadata. |
| META-02 | Show sources omitted by metadata deduplication. Do not treat equal metadata as proof of equal contents. | Use two different recordings with equal author and title metadata. Show both source paths and keep both sources. |
