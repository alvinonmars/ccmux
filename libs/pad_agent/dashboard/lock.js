/**
 * KidPad Lock Screen — Displayed when the tablet is locked.
 *
 * Shows a friendly reason-specific message (eye break, bedtime, daily limit,
 * etc.) and periodically checks state for unlock. Eye-break locks include a
 * 20-second countdown with a gentle pulse animation.
 *
 * Bedtime lock shows tomorrow's schedule preview.
 *
 * All fully.* calls are guarded for development in a normal browser.
 */

(function () {
    'use strict';

    // --------------- FK bridge detection ---------------
    var hasFK = typeof fully !== 'undefined';

    // --------------- Reason from URL query ---------------
    var params = new URLSearchParams(window.location.search);
    var currentReason = params.get('reason') || 'unknown';

    // --------------- Reason priority & messages ---------------

    /**
     * Priority order: higher-priority reasons take visual precedence when
     * multiple lock reasons exist simultaneously.
     */
    var REASON_PRIORITY = [
        'bedtime',
        'daily_limit',
        'session_limit',
        'eye_break',
        'heartbeat_timeout',
        'manual'
    ];

    var MESSAGES = {
        bedtime: {
            icon: '\uD83C\uDF19', // moon
            text: "It's bedtime. Sweet dreams!",
            dark: true
        },
        daily_limit: {
            icon: '\u2B50', // star
            text: "You've used all your screen time today! Great job!"
        },
        session_limit: {
            icon: '\u23F0', // alarm clock
            text: 'Time for a break! Come back soon.'
        },
        eye_break: {
            icon: '\uD83D\uDC40', // eyes
            text: 'Look at something far away!'
        },
        heartbeat_timeout: {
            icon: '\uD83D\uDD0C', // plug
            text: 'Connection lost. Ask a parent for help.'
        },
        manual: {
            icon: '\uD83D\uDD12', // lock
            text: 'Tablet is locked by a parent.'
        },
        unknown: {
            icon: '\uD83D\uDD12', // lock
            text: 'Tablet is locked.'
        }
    };

    // --------------- State loading ---------------

    var cachedState = null;

    function loadState() {
        if (hasFK) {
            try {
                var raw = fully.getStringSetting('kidpad_state');
                if (raw) return JSON.parse(raw);
            } catch (e) { /* fall through */ }
        }
        fetchStateFile();
        return cachedState;
    }

    function fetchStateFile() {
        try {
            var xhr = new XMLHttpRequest();
            xhr.open('GET', 'state.json?t=' + Date.now(), true);
            xhr.timeout = 3000;
            xhr.onload = function () {
                if (xhr.status === 200 || xhr.status === 0) {
                    try {
                        cachedState = JSON.parse(xhr.responseText);
                        // Update tomorrow schedule on async load
                        if (currentReason === 'bedtime') {
                            showTomorrowSchedule(cachedState);
                        }
                    } catch (e) {}
                }
            };
            xhr.send();
        } catch (e) {}
    }

    // --------------- Reason helpers ---------------

    /**
     * Given an array of lock reasons, return the highest-priority one.
     */
    function getPrimaryReason(reasons) {
        if (!Array.isArray(reasons) || reasons.length === 0) return 'unknown';
        for (var i = 0; i < REASON_PRIORITY.length; i++) {
            if (reasons.indexOf(REASON_PRIORITY[i]) !== -1) {
                return REASON_PRIORITY[i];
            }
        }
        return reasons[0];
    }

    /**
     * Update the lock screen UI for the given reason.
     */
    function showReason(reason) {
        var msg = MESSAGES[reason] || MESSAGES.unknown;

        var iconEl = document.getElementById('icon');
        if (iconEl) iconEl.textContent = msg.icon;

        var messageEl = document.getElementById('message');
        if (messageEl) messageEl.textContent = msg.text;

        // Bedtime dark mode
        var lockScreen = document.getElementById('lockScreen');
        if (lockScreen) {
            if (msg.dark) {
                lockScreen.classList.add('bedtime-mode');
            } else {
                lockScreen.classList.remove('bedtime-mode');
            }
        }

        // Show countdown only for eye_break
        var countdownEl = document.getElementById('countdown');
        if (countdownEl && reason !== 'eye_break') {
            countdownEl.style.display = 'none';
        }

        // Show/hide tomorrow schedule
        var tomorrowEl = document.getElementById('tomorrowSchedule');
        if (tomorrowEl) {
            tomorrowEl.style.display = (reason === 'bedtime') ? 'block' : 'none';
        }
    }

    // --------------- Tomorrow's schedule (bedtime) ---------------

    function showTomorrowSchedule(state) {
        var container = document.getElementById('tomorrowSchedule');
        var list = document.getElementById('tomorrowList');
        if (!container || !list) return;

        var schedule = state && state.tomorrow_schedule;
        if (!Array.isArray(schedule) || schedule.length === 0) {
            container.style.display = 'none';
            return;
        }

        container.style.display = 'block';
        list.innerHTML = '';

        for (var i = 0; i < schedule.length; i++) {
            var item = schedule[i];
            var div = document.createElement('div');
            div.className = 'tomorrow-item';

            var timeSpan = document.createElement('span');
            timeSpan.className = 'tomorrow-time';
            timeSpan.textContent = item.time || '';

            var iconSpan = document.createElement('span');
            iconSpan.className = 'tomorrow-icon';
            iconSpan.textContent = item.icon || '';

            var nameSpan = document.createElement('span');
            nameSpan.className = 'tomorrow-name';
            nameSpan.textContent = item.name || '';

            div.appendChild(timeSpan);
            div.appendChild(iconSpan);
            div.appendChild(nameSpan);
            list.appendChild(div);
        }
    }

    // --------------- Eye break countdown ---------------

    var EYE_BREAK_SECONDS = 20;
    var countdownInterval = null;

    function startEyeBreakCountdown() {
        var remaining = EYE_BREAK_SECONDS;
        var timer = document.getElementById('countdown');
        if (!timer) return;

        timer.style.display = 'block';
        timer.textContent = remaining + 's';

        if (countdownInterval) {
            clearInterval(countdownInterval);
            countdownInterval = null;
        }

        countdownInterval = setInterval(function () {
            remaining--;
            if (remaining > 0) {
                timer.textContent = remaining + 's';
            } else {
                clearInterval(countdownInterval);
                countdownInterval = null;
                timer.textContent = 'Done!';
                timer.style.animation = 'none';
            }
        }, 1000);
    }

    // --------------- Navigation helpers ---------------

    function goToDashboard() {
        if (hasFK) {
            try {
                fully.loadStartUrl();
            } catch (e) {
                window.location.href = 'index.html';
            }
        } else {
            window.location.href = 'index.html';
        }
    }

    // --------------- Initialize ---------------

    showReason(currentReason);
    if (currentReason === 'eye_break') {
        startEyeBreakCountdown();
    }

    // Load state for tomorrow schedule on bedtime
    if (currentReason === 'bedtime') {
        var initState = loadState();
        if (initState) {
            showTomorrowSchedule(initState);
        }
    }

    // --------------- Periodic state refresh (5s) ---------------

    setInterval(function () {
        var state = loadState();
        if (!state) return;

        // Detect unlock: go back to dashboard
        if (
            !state.is_locked ||
            !Array.isArray(state.lock_reasons) ||
            state.lock_reasons.length === 0
        ) {
            goToDashboard();
            return;
        }

        // Update displayed reason if it changed
        var newReason = getPrimaryReason(state.lock_reasons);
        if (newReason !== currentReason) {
            currentReason = newReason;
            showReason(currentReason);
            if (currentReason === 'eye_break' && !countdownInterval) {
                startEyeBreakCountdown();
            }
        }

        // Update tomorrow schedule if bedtime
        if (currentReason === 'bedtime') {
            showTomorrowSchedule(state);
        }
    }, 5000);

    // Also refresh on visibility change
    document.addEventListener('visibilitychange', function () {
        if (!document.hidden) {
            var state = loadState();
            if (!state) return;
            if (
                !state.is_locked ||
                !Array.isArray(state.lock_reasons) ||
                state.lock_reasons.length === 0
            ) {
                goToDashboard();
            }
        }
    });
})();
