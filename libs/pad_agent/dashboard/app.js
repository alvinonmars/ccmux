/**
 * KidPad Dashboard v3 — Main screen logic
 *
 * Reads state from Fully Kiosk Browser's string setting, updates the UI
 * with schedule, weather, homework, and screen time data, and redirects
 * to the lock screen when the tablet is locked.
 *
 * All fully.* calls are guarded so the page degrades gracefully in a
 * normal browser (useful for development / testing).
 */

(function () {
    'use strict';

    // --------------- FK bridge detection ---------------
    var hasFK = typeof fully !== 'undefined';

    // --------------- State loading ---------------

    var cachedState = null;

    /**
     * Read and parse the kidpad_state JSON.
     * Primary: FK JS API (fully.getStringSetting).
     * Fallback: fetch state.json from same directory (for non-PLUS FK).
     */
    function loadState() {
        if (hasFK) {
            try {
                var raw = fully.getStringSetting('kidpad_state');
                if (raw) return JSON.parse(raw);
            } catch (e) { /* fall through to file fallback */ }
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
                        updateDashboard(cachedState);
                        checkHeartbeat(cachedState);
                    } catch (e) { /* ignore parse errors */ }
                }
            };
            xhr.send();
        } catch (e) { /* ignore fetch errors */ }
    }

    // --------------- Greeting ---------------

    function getGreeting() {
        var h = new Date().getHours();
        if (h < 12) return 'Good morning';
        if (h < 17) return 'Good afternoon';
        return 'Good evening';
    }

    // --------------- Dashboard update ---------------

    function updateDashboard(state) {
        if (!state) return;

        // Header: weather + date
        updateHeader(state);

        // Greeting
        var name = state.child_name || 'friend';
        var greetingEl = document.getElementById('greeting');
        if (greetingEl) {
            greetingEl.textContent = getGreeting() + ', ' + name + '!';
        }

        // Reminder banner
        updateReminder(state);

        // Schedule card
        updateSchedule(state);

        // Progress bar
        var dailyLimit = state.daily_limit || 60;
        var active = state.active_minutes || 0;
        var pct = Math.min(100, (active / dailyLimit) * 100);
        var fill = document.getElementById('progress');
        if (fill) {
            fill.style.width = pct + '%';
            if (pct < 50) {
                fill.style.backgroundColor = '#4CAF50';
            } else if (pct < 80) {
                fill.style.backgroundColor = '#FFC107';
            } else {
                fill.style.backgroundColor = '#F44336';
            }
        }

        // Time text
        var timeTextEl = document.getElementById('timeText');
        if (timeTextEl) {
            timeTextEl.textContent =
                Math.round(active) + ' / ' + dailyLimit + ' min';
        }

        // Session text
        var sessionEl = document.getElementById('sessionText');
        if (sessionEl) {
            var sessionMin = state.session_minutes || 0;
            var sessionLimit = state.session_limit;
            var sessionStr = 'Session: ' + Math.round(sessionMin) + ' min';
            if (typeof sessionLimit === 'number' && sessionLimit > 0) {
                sessionStr += ' / ' + sessionLimit + ' min';
            }
            sessionEl.textContent = sessionStr;
        }

        // Eye break info (inline)
        var ebEl = document.getElementById('eyeBreak');
        if (ebEl) {
            var ebMin = state.next_eye_break_minutes;
            if (typeof ebMin === 'number' && ebMin >= 0) {
                ebEl.textContent =
                    'Eye break in: ' + Math.round(ebMin) + ' min';
            } else {
                ebEl.textContent = 'Eye break in: -- min';
            }
        }

        // Homework footer
        updateHomework(state);
    }

    // --------------- Header (weather + date) ---------------

    function updateHeader(state) {
        var weatherEl = document.getElementById('weatherInfo');
        if (weatherEl) {
            if (state.weather && typeof state.weather.temp === 'number') {
                weatherEl.textContent =
                    (state.weather.icon || '') + ' ' +
                    state.weather.temp + '\u00B0C';
            } else {
                weatherEl.textContent = '';
            }
        }

        var dateEl = document.getElementById('dateInfo');
        if (dateEl) {
            dateEl.textContent = state.date_display || '';
        }
    }

    // --------------- Schedule card ---------------

    function updateSchedule(state) {
        var card = document.getElementById('scheduleCard');
        var list = document.getElementById('scheduleList');
        if (!card || !list) return;

        var schedule = state.schedule;
        if (!Array.isArray(schedule) || schedule.length === 0) {
            card.style.display = 'none';
            return;
        }

        card.style.display = 'block';
        list.innerHTML = '';

        for (var i = 0; i < schedule.length; i++) {
            var item = schedule[i];
            var div = document.createElement('div');
            div.className = 'schedule-item';

            var timeSpan = document.createElement('span');
            timeSpan.className = 'schedule-time';
            timeSpan.textContent = item.time || '';

            var iconSpan = document.createElement('span');
            iconSpan.className = 'schedule-icon';
            iconSpan.textContent = item.icon || '';

            var nameSpan = document.createElement('span');
            nameSpan.className = 'schedule-name';
            nameSpan.textContent = item.name || '';

            div.appendChild(timeSpan);
            div.appendChild(iconSpan);
            div.appendChild(nameSpan);
            list.appendChild(div);
        }
    }

    // --------------- Reminder banner ---------------

    function updateReminder(state) {
        var banner = document.getElementById('reminderBanner');
        if (!banner) return;

        if (!state.reminder || !state.reminder.text) {
            banner.style.display = 'none';
            return;
        }

        banner.style.display = 'flex';
        var iconEl = document.getElementById('reminderIcon');
        if (iconEl) iconEl.textContent = state.reminder.icon || '';
        var textEl = document.getElementById('reminderText');
        if (textEl) textEl.textContent = state.reminder.text;
    }

    // --------------- Homework footer ---------------

    function updateHomework(state) {
        var footer = document.getElementById('homeworkFooter');
        var textEl = document.getElementById('homeworkText');
        if (!footer || !textEl) return;

        var homework = state.homework;
        if (!Array.isArray(homework) || homework.length === 0) {
            footer.style.display = 'none';
            return;
        }

        footer.style.display = 'block';
        var parts = [];
        for (var i = 0; i < homework.length; i++) {
            var hw = homework[i];
            parts.push(
                (hw.icon || '') + ' ' +
                (hw.subject || '') + ' (' + (hw.due || '') + ')'
            );
        }
        textEl.textContent = '\uD83D\uDCDA Homework: ' + parts.join(', ');
    }

    // --------------- Heartbeat check (seq-based) ---------------

    var lastSeq = -1;
    var lastSeqTime = Date.now();

    function checkHeartbeat(state) {
        if (!state) return;

        var seq = state.seq;
        if (typeof seq !== 'number') return;

        if (seq !== lastSeq) {
            lastSeq = seq;
            lastSeqTime = Date.now();
        }

        var timeout = state.heartbeat_timeout_ms || 300000;
        if (Date.now() - lastSeqTime > timeout) {
            window.location.href =
                'lock.html?reason=' + encodeURIComponent('heartbeat_timeout');
        }
    }

    // --------------- App launcher ---------------

    window.launchApp = function launchApp(packageName) {
        if (!packageName) return;
        if (hasFK) {
            try {
                fully.startApplication(packageName);
            } catch (e) {}
        }
    };

    // --------------- Main refresh loop ---------------

    function loadAndRefresh() {
        var state = loadState();
        if (!state) return;

        if (
            state.is_locked &&
            Array.isArray(state.lock_reasons) &&
            state.lock_reasons.length > 0
        ) {
            var reason = state.lock_reasons[0];
            window.location.href =
                'lock.html?reason=' + encodeURIComponent(reason);
            return;
        }

        updateDashboard(state);
        checkHeartbeat(state);
    }

    document.addEventListener('visibilitychange', function () {
        if (!document.hidden) {
            loadAndRefresh();
        }
    });

    if (hasFK) {
        document.addEventListener('onFullyEvent', function (e) {
            if (e && e.detail === 'screenOn') {
                loadAndRefresh();
            }
        });
    }

    loadAndRefresh();
    setInterval(loadAndRefresh, 10000);
})();
