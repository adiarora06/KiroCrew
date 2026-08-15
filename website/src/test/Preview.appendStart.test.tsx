/**
 * Regression guard for the Notes app's "click below the note to append"
 * insertion point.
 *
 * Background
 * ----------
 * `body.split('\n')` produces a trailing EMPTY element whenever `body` ends
 * with `\n` (String.split behavior: N newlines -> N+1 elements) -- that
 * element is not a real line of content. `appendStart` used to be plain
 * `lines.length`, over-counting by one in exactly that case. Clicking the
 * append region then opened an insertion at one slot PAST where
 * `commitBlockEdit`/`splitBlockEdit` (MdNotebookPage.tsx) splice against
 * their OWN `contentRef.current.split('\n')`, landing the new text AFTER the
 * phantom empty element instead of at it -- so appending to any note whose
 * file ends with a trailing newline (the common case for anything that's
 * been through git) silently wrote an extra blank line before the new text.
 *
 * `Preview` alone doesn't own the splice (that's the parent component), so
 * these tests pin the CONTRACT `Preview` must uphold: `onStartEdit` is
 * called with the append-region's `(start, end)` pointing at the number of
 * REAL lines in the body, not the raw split-array length.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import { Preview } from '../apps/md-notebook/Preview'

function renderAndClickAppend(content: string) {
  const onStartEdit = vi.fn()
  render(
    <Preview
      content={content}
      onToggleCheckbox={vi.fn()}
      editRange={null}
      onStartEdit={onStartEdit}
      onCommitEdit={vi.fn()}
      onCancelEdit={vi.fn()}
      onSplitEdit={vi.fn()}
    />,
  )
  // The append region is always the LAST rendered block (every content block
  // above it also carries role="button" for click-to-edit).
  const buttons = screen.getAllByRole('button')
  fireEvent.click(buttons[buttons.length - 1])
  return onStartEdit
}

/**
 * Drives the real click -> setEditBlock -> re-render sequence `MdNotebookPage`
 * performs, instead of asserting `onStartEdit`'s arguments in isolation: a
 * `Preview` that only agrees with its OWN emitted range on `(start, end)`
 * would still pass the tests above while double-mounting a `BlockEditor`
 * once that range is actually fed back in as `editRange` (see the two
 * `mounts exactly one editor` tests below).
 */
function renderClickAndFollowUp(content: string) {
  const onStartEdit = vi.fn()
  const view = render(
    <Preview
      content={content}
      onToggleCheckbox={vi.fn()}
      editRange={null}
      onStartEdit={onStartEdit}
      onCommitEdit={vi.fn()}
      onCancelEdit={vi.fn()}
      onSplitEdit={vi.fn()}
    />,
  )
  const buttons = screen.getAllByRole('button')
  fireEvent.click(buttons[buttons.length - 1])
  const [start, end] = onStartEdit.mock.calls[0] as [number, number]
  view.rerender(
    <Preview
      content={content}
      onToggleCheckbox={vi.fn()}
      editRange={{ start, end }}
      onStartEdit={onStartEdit}
      onCommitEdit={vi.fn()}
      onCancelEdit={vi.fn()}
      onSplitEdit={vi.fn()}
    />,
  )
  return view
}

describe('Preview append-region insertion point', () => {
  it('a single line with a trailing newline appends at index 1, not 2', () => {
    // "Hello\n".split('\n') === ["Hello", ""] -- the trailing "" is the
    // phantom, not a second real line.
    const onStartEdit = renderAndClickAppend('Hello\n')
    expect(onStartEdit).toHaveBeenCalledWith(1, 0)
  })

  it('a single line with NO trailing newline also appends at index 1', () => {
    // Must agree with the trailing-newline case above: the file has exactly
    // one real line either way.
    const onStartEdit = renderAndClickAppend('Hello')
    expect(onStartEdit).toHaveBeenCalledWith(1, 0)
  })

  it('a brand-new empty note appends at index 0', () => {
    const onStartEdit = renderAndClickAppend('')
    expect(onStartEdit).toHaveBeenCalledWith(0, -1)
  })

  it('a real trailing blank line is still counted, only the phantom is not', () => {
    // "Hello\n\n".split('\n') === ["Hello", "", ""] -- two real lines
    // (Hello, then a blank line), plus the phantom from the final \n.
    const onStartEdit = renderAndClickAppend('Hello\n\n')
    expect(onStartEdit).toHaveBeenCalledWith(2, 1)
  })

  it('multiple lines with a trailing newline append after the last real line', () => {
    const onStartEdit = renderAndClickAppend('one\ntwo\nthree\n')
    expect(onStartEdit).toHaveBeenCalledWith(3, 2)
  })
})

describe('Preview append-region editor mounting', () => {
  /**
   * `appendStart` lands on the SAME index as the phantom trailing line's own
   * rendered block for every trailing-newline-terminated note (the common
   * case) and for a brand-new empty note. Both `blk()` and the append slot
   * used to key off `editRange.start` alone, so the click that opens the
   * append editor also matched the phantom line's own block and mounted a
   * SECOND `BlockEditor` there; its autofocus stole focus from the first,
   * whose blur-commit reset `editRange` to null and unmounted both before a
   * keystroke landed -- append going from "adds a spurious blank line" to
   * "does nothing".
   */
  it('clicking the append region mounts exactly one editor, and it stays mounted', () => {
    const view = renderClickAndFollowUp('Hello\n')
    expect(screen.getAllByRole('textbox')).toHaveLength(1)
    // React ran every mounted editor's autofocus effect by now; if a second
    // one had mounted and stolen focus, the blur-commit path would already
    // have reset editRange to null and unmounted this one too.
    expect(view.container.querySelector('textarea')).not.toBeNull()
  })

  it('clicking the append region on a brand-new empty note also mounts exactly one editor', () => {
    renderClickAndFollowUp('')
    expect(screen.getAllByRole('textbox')).toHaveLength(1)
  })

  it('clicking the phantom trailing line itself (not the append region) also mounts exactly one editor', () => {
    // Distinct trigger, same collision: onStartEdit(1, 1) is a genuine
    // same-line edit (end === start, not an insertion), but its `start` is
    // still equal to `appendStart` -- the append slot's own condition must
    // not fire for this shape either.
    const onStartEdit = vi.fn()
    const view = render(
      <Preview
        content={'Hello\n'}
        onToggleCheckbox={vi.fn()}
        editRange={{ start: 1, end: 1 }}
        onStartEdit={onStartEdit}
        onCommitEdit={vi.fn()}
        onCancelEdit={vi.fn()}
        onSplitEdit={vi.fn()}
      />,
    )
    expect(screen.getAllByRole('textbox')).toHaveLength(1)
    expect(view.container.querySelector('textarea')).not.toBeNull()
  })
})
