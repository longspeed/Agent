(function () {
  'use strict';

  const STORAGE_KEY = 'sendkeep:outreach-desk';

  function readStoredState() {
    try {
      const value = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || '{}');
      return {
        selection: typeof value.selection === 'string' ? value.selection : '',
        filter: ['all', 'reply', 'promise', 'due', 'verify'].includes(value.filter) ? value.filter : 'all',
      };
    } catch (_error) {
      return { selection: '', filter: 'all' };
    }
  }

  function persistState(state) {
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (_error) {
      // The desk remains usable when session storage is blocked.
    }
  }

  function create(options) {
    const shell = options.shell;
    const queue = options.queue;
    const filters = options.filters;
    const detail = options.detail;
    const back = options.back;
    const stored = readStoredState();
    const state = {
      selection: stored.selection,
      filter: stored.filter,
      mobileDetailOpen: false,
      orderedKeys: [],
    };

    function save() {
      persistState({ selection: state.selection, filter: state.filter });
    }

    function setSelection(key, { openDetail = false, focusQueue = false } = {}) {
      state.selection = key || '';
      state.mobileDetailOpen = Boolean(openDetail && key);
      shell?.classList.toggle('is-detail-open', state.mobileDetailOpen);
      save();
      options.onSelectionChange(state.selection);
      if (focusQueue) {
        requestAnimationFrame(() => queue?.querySelector(`[data-guided-key="${CSS.escape(state.selection)}"]`)?.focus());
      }
    }

    function moveSelection(delta) {
      if (!state.orderedKeys.length) return;
      const current = Math.max(0, state.orderedKeys.indexOf(state.selection));
      const next = Math.min(state.orderedKeys.length - 1, Math.max(0, current + delta));
      setSelection(state.orderedKeys[next], { focusQueue: true });
    }

    queue?.addEventListener('click', (event) => {
      const item = event.target.closest('[data-guided-key]');
      if (!item) return;
      setSelection(item.dataset.guidedKey, { openDetail: true });
    });

    queue?.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        moveSelection(1);
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        moveSelection(-1);
      } else if (event.key === 'Home' && state.orderedKeys.length) {
        event.preventDefault();
        setSelection(state.orderedKeys[0], { focusQueue: true });
      } else if (event.key === 'End' && state.orderedKeys.length) {
        event.preventDefault();
        setSelection(state.orderedKeys[state.orderedKeys.length - 1], { focusQueue: true });
      }
    });

    filters?.addEventListener('click', (event) => {
      const filter = event.target.closest('[data-guided-filter]');
      if (!filter) return;
      state.filter = filter.dataset.guidedFilter;
      state.selection = '';
      save();
      options.onFilterChange(state.filter);
    });

    back?.addEventListener('click', () => {
      state.mobileDetailOpen = false;
      shell?.classList.remove('is-detail-open');
      requestAnimationFrame(() => queue?.querySelector('[aria-current="true"]')?.focus());
    });

    document.addEventListener('keydown', (event) => {
      const target = event.target;
      const isTyping = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target?.isContentEditable;
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        const action = detail?.querySelector('.rc-send:not([disabled]), .commitment-confirm:not([disabled]), .commitment-complete:not([disabled])');
        if (action) {
          event.preventDefault();
          action.click();
        }
        return;
      }
      if (!isTyping && event.key.toLowerCase() === 'e') {
        event.preventDefault();
        moveSelection(1);
      }
    });

    return {
      get selection() { return state.selection; },
      get filter() { return state.filter; },
      setFilter(filter) {
        state.filter = filter;
        save();
      },
      sync(items, selection) {
        state.orderedKeys = items.map((item) => item.key);
        state.selection = selection || '';
        if (!state.selection) {
          state.mobileDetailOpen = false;
          shell?.classList.remove('is-detail-open');
        }
        save();
      },
      showQueue() {
        state.mobileDetailOpen = false;
        shell?.classList.remove('is-detail-open');
      },
    };
  }

  window.SendkeepOutreachDesk = { create };
}());
