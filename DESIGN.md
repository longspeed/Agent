# Sendkeep product interface

Sendkeep is a calm operating desk for handling replies, promises, and one-time follow-ups. The product surface should help an operator take the next safe action within seconds. It is not an analytics dashboard.

## Product hierarchy

1. Decision queue: replies first, then unknown send outcomes, promises, and due follow-ups.
2. Selected item: conversation context, editable content, consequence, and actions.
3. Gmail health: connection and worker freshness remain visible without competing with the work.

Evaluation metrics, dogfood inputs, documentation, and pipeline reports do not belong on the primary outreach desk.

## Color tokens

| Token | Value | Meaning |
|---|---|---|
| `--accent` | `#C2491D` | Interactive controls, focus, and selection only |
| `--accent-hover` | `#A93E17` | Hovered primary action |
| `--success` | `#397152` | Completed or healthy states only |
| `--warning` | `#8C6B24` | Promises, waiting, and caution |
| `--danger` | `#A83232` | Errors, destructive actions, and blocked states |
| `--info` | `#527184` | Prepared content and neutral verification information |
| `--text-muted` | `#6E7368` | Secondary text with at least AA contrast on paper surfaces |

Orange never communicates success. Green never invites an action. Every keyboard focus indicator uses `--focus-ring` derived from the accent.

## Typography and controls

- Outfit: headings, navigation, labels, and buttons.
- Inter: messages, drafts, inputs, and longer utility copy.
- Body and editable content: 14px minimum in dense desktop UI, 16px when reading is the main task.
- Interactive targets: 44 by 44 CSS pixels minimum.
- Large counts use tabular numerals.

## Desk components

- `guided-review-shell`: the sole primary outreach workspace.
- `guided-queue-item`: a real decision, never a decorative card.
- `guided-system-state`: inline loading, bootstrap, stale, partial-error, and reconnect state.
- `guided-consequence`: one sentence explaining what the primary action changes.
- `guided-kind`: semantic Reply, Verify, Promise, or Due label.

Item verbs are intentionally different:

- Reply: Send reply, Ignore reply. Ignoring clears the Sendkeep queue item; it does not delete or archive the Gmail message.
- Promise: Confirm, Edit, Discard.
- Due: Send follow-up, Snooze, Cancel follow-up.
- Verify: Open Gmail; resending stays disabled.

## Responsive behavior

- At 681px and wider, show queue and detail side by side.
- At 680px and narrower, show the queue first and open the detail as a full-width second view with a Back control.
- Do not stack a shortened queue above an editor.

## Keyboard and motion

- Up/Down and Home/End move through the queue.
- `E` advances without sending.
- `Ctrl+Enter` or `Command+Enter` deliberately invokes the selected primary action.
- Sending never shares a shortcut with navigation.
- Reduced-motion mode removes transforms and nonessential transitions.
