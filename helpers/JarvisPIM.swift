// Jarvis's window onto Calendar and Reminders, through Apple's EventKit.
//
//   JarvisPIM events <start-epoch> <end-epoch>     -> JSON list of events (repeating ones included)
//   JarvisPIM reminders                            -> JSON list of incomplete reminders
//   JarvisPIM add-reminder <title> [<due-epoch>]   -> JSON {"ok":true}
//   JarvisPIM add-event <title> <start-epoch> <minutes> -> JSON {"ok":true}
//
// It reads, and adds reminders and events (with no invitees). It never edits or deletes anything.
// macOS asks once for permission ("Terminal would like to access your calendars").
// Output is JSON on stdout; errors are {"error": "..."} with exit code 1.

import EventKit
import Foundation

let store = EKEventStore()
let args = CommandLine.arguments

func emit(_ obj: Any) {
    if let data = try? JSONSerialization.data(withJSONObject: obj, options: []),
       let s = String(data: data, encoding: .utf8) { print(s) }
}

func fail(_ msg: String) -> Never {
    emit(["error": msg])
    exit(1)
}

func access(_ type: EKEntityType) -> Bool {
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    if #available(macOS 14.0, *) {
        if type == .event {
            store.requestFullAccessToEvents { ok, _ in granted = ok; sem.signal() }
        } else {
            store.requestFullAccessToReminders { ok, _ in granted = ok; sem.signal() }
        }
    } else {
        store.requestAccess(to: type) { ok, _ in granted = ok; sem.signal() }
    }
    if sem.wait(timeout: .now() + 60) == .timedOut { return false }
    return granted
}

guard args.count >= 2 else { fail("usage: events|reminders|add-reminder") }

switch args[1] {
case "events":
    guard args.count == 4, let a = Double(args[2]), let b = Double(args[3]) else { fail("events <start> <end>") }
    guard access(.event) else { fail("no-permission") }
    let pred = store.predicateForEvents(withStart: Date(timeIntervalSince1970: a),
                                        end: Date(timeIntervalSince1970: b), calendars: nil)
    let events = store.events(matching: pred).sorted { $0.startDate < $1.startDate }
    emit(events.prefix(30).map { e -> [String: Any] in
        ["title": e.title ?? "(untitled)",
         "start": e.startDate.timeIntervalSince1970,
         "end": e.endDate.timeIntervalSince1970,
         "all_day": e.isAllDay,
         "location": e.location ?? "",
         "calendar": e.calendar?.title ?? ""]
    })

case "reminders":
    guard access(.reminder) else { fail("no-permission") }
    let pred = store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: nil)
    let sem = DispatchSemaphore(value: 0)
    var out: [[String: Any]] = []
    store.fetchReminders(matching: pred) { items in
        for r in (items ?? []).prefix(40) {
            var d: [String: Any] = ["title": r.title ?? "(untitled)", "list": r.calendar?.title ?? ""]
            if let due = r.dueDateComponents, let date = Calendar.current.date(from: due) {
                d["due"] = date.timeIntervalSince1970
            }
            out.append(d)
        }
        sem.signal()
    }
    _ = sem.wait(timeout: .now() + 20)
    emit(out)

case "add-reminder":
    guard args.count >= 3 else { fail("add-reminder <title> [due]") }
    guard access(.reminder) else { fail("no-permission") }
    guard let cal = store.defaultCalendarForNewReminders() else { fail("no-reminders-list") }
    let r = EKReminder(eventStore: store)
    r.title = String(args[2].prefix(200))
    r.calendar = cal
    if args.count >= 4, let due = Double(args[3]) {
        let date = Date(timeIntervalSince1970: due)
        r.dueDateComponents = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute], from: date)
        r.addAlarm(EKAlarm(absoluteDate: date))
    }
    do {
        try store.save(r, commit: true)
        emit(["ok": true, "list": cal.title])
    } catch {
        fail("save-failed")
    }

case "add-event":
    guard args.count == 5, let start = Double(args[3]), let minutes = Double(args[4]),
          minutes > 0, minutes <= 24 * 60 else { fail("add-event <title> <start> <minutes>") }
    guard access(.event) else { fail("no-permission") }
    guard let cal = store.defaultCalendarForNewEvents else { fail("no-calendar") }
    let e = EKEvent(eventStore: store)
    e.title = String(args[2].prefix(200))
    e.startDate = Date(timeIntervalSince1970: start)
    e.endDate = e.startDate.addingTimeInterval(minutes * 60)
    e.calendar = cal
    e.addAlarm(EKAlarm(relativeOffset: -15 * 60))
    do {
        try store.save(e, span: .thisEvent, commit: true)
        emit(["ok": true, "calendar": cal.title])
    } catch {
        fail("save-failed")
    }

default:
    fail("unknown command")
}
