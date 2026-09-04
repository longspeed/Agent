(function () {
  'use strict';

  const labels = {
    reply: 'REPLY',
    verify: 'VERIFY',
    promise: 'PROMISE',
    due: 'DUE',
  };

  const consequences = {
    reply: 'Sending replies in this Gmail thread and clears this item.',
    promise: 'Confirming schedules this promise. Discarding removes the detection.',
    verify: 'Gmail did not confirm the result. Do not resend until you verify Sent mail.',
    due: 'Sending uses this contact’s one follow-up. It will not be scheduled again.',
  };

  function heading(tab) {
    if (tab === 'promises') {
      return {
        title: 'Promise review',
        subtitle: 'Confirm the promise or mark the detection incorrect. Nothing schedules until you confirm.',
      };
    }
    if (tab === 'follow-ups') {
      return {
        title: 'Follow-ups due',
        subtitle: 'Each contact gets one follow-up. Send it, snooze it, or cancel it.',
      };
    }
    return {
      title: 'Today’s review',
      subtitle: 'Replies first. Resolving one may clear a promise or follow-up automatically.',
    };
  }

  function queueItem(item, selected, escapeHtml, relativeTime) {
    const active = item.key === selected;
    return `
      <button type="button" class="guided-queue-item" role="option" tabindex="${active ? '0' : '-1'}" aria-selected="${active}" aria-current="${active}" data-guided-key="${escapeHtml(item.key)}">
        <span class="guided-kind ${item.type}">${labels[item.type] || 'ITEM'}</span>
        <span class="guided-item-copy"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.summary)}</span></span>
        <span class="guided-item-time">${escapeHtml(relativeTime(item.time))}</span>
      </button>`;
  }

  function empty(loading) {
    return loading
      ? '<div class="guided-empty"><div><strong>Checking recent Gmail activity…</strong>Your queue will stay in place while Sendkeep looks for work.</div></div>'
      : '<div class="guided-empty"><div><strong>All clear</strong>There is nothing in this lane that needs a decision.</div></div>';
  }

  function detailEmpty(loading) {
    return loading
      ? '<div class="guided-empty"><div><strong>Loading today’s work</strong>This usually takes a moment.</div></div>'
      : '<div class="guided-empty"><div><strong>You are caught up.</strong>New replies, promises, and due follow-ups will appear automatically.</div></div>';
  }

  window.SendkeepOutreachDeskRenderers = {
    consequence(type) { return consequences[type] || consequences.reply; },
    detailEmpty,
    empty,
    heading,
    queueItem,
  };
}());
