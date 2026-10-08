# Open work

Keep open tasks in this file.
Remove a task after its tests and documentation are complete.
Each task must state the required result and its acceptance check.

## Priority 1: Operation recovery

- [ ] Add a persistent transaction journal for organize and undo operations.
  - Record intent before each file change.
  - Recover an interrupted operation after process failure or power loss.
  - Test interruption before and after each file change and log replacement.
- [ ] Add a collection lock for commands that change data.
  - Include organize, cleanup, rename, corrections, and undo.
  - Reject a second writer without changes to collection data.
  - Test two separate processes and recovery from a stale lock.

## Priority 2: Performance

- [ ] Measure scan cache cost on a large collection and a network share.
  - Compare scan time, directory reads, and tag reads with and without the cache.
  - Keep cache invalidation correct for file and directory changes.
- [ ] Add progress counts for content verification during cleanup.
  - Show the checked bytes and the total bytes in interactive terminals.
  - Keep redirected output free of progress animation.

## Priority 3: Metadata review

- [ ] Add a review step for uncertain author and title matches.
  - Show the selected metadata source and the proposed destination.
  - Keep the source until the user accepts the proposed metadata.
  - Test ambiguous two-part names and long-title destination conflicts.
