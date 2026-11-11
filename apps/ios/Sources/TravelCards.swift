import SwiftUI
import UIKit

// v3.9.40 出行: the cards restaurant_booking and flight_search put on a task's result (result.cards, server travel.py).
//  * 订餐厅: name, address, ★ / 人均, the suggested date · time · party size, 「打开大众点评预订」/「打开美团」.
//  * 机票比价: the merged rows (price, flight, times, platform, 含税?) sorted by price, every platform's status.
// A button / row carries {app, web, prefer}. AppLink.open: prefer=app -> the app's scheme (大众点评 / 美团 shop page or
// search; listed in LSApplicationQueriesSchemes); not installed -> the https link as a universal link only (opens the
// platform's app when it claims that domain) -> Safari. aiw never books or pays: the user confirms in the app.
// The schemes are from public URL-scheme collections and were not tried on a device (see README v3.9.40).

enum AppLink {
    @MainActor static func open(_ l: JSON) {
        open(app: l.s("app"), web: l.s("web"), preferApp: l.s("prefer") == "app")
    }

    @MainActor static func open(app: String, web: String, preferApp: Bool) {
        let webURL = URL(string: web)
        func toWeb() {
            guard let w = webURL else { return }
            if w.scheme == "https" {
                UIApplication.shared.open(w, options: [.universalLinksOnly: true]) { ok in
                    if !ok { UIApplication.shared.open(w) }
                }
            } else {
                UIApplication.shared.open(w)
            }
        }
        if preferApp, !app.isEmpty, let a = URL(string: app) {
            UIApplication.shared.open(a, options: [:]) { ok in
                if !ok { toWeb() }
            }
        } else {
            toWeb()
        }
    }
}

private func num(_ j: JSON, _ k: String) -> Double? {
    if let d = j[k] as? Double { return d }
    if let i = j[k] as? Int { return Double(i) }
    if let n = j[k] as? NSNumber { return n.doubleValue }
    return nil
}

private func yuan(_ v: Double?) -> String {
    guard let v = v else { return "—" }
    return v == v.rounded() ? "¥\(Int(v))" : String(format: "¥%.2f", v)
}

struct TravelCardsView: View {
    let cards: [JSON]
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(cards.indices, id: \.self) { i in
                let c = cards[i]
                if c.s("kind") == "restaurant" {
                    RestaurantCardView(card: c)
                } else if c.s("kind") == "flights" {
                    FlightCardView(card: c)
                }
            }
        }
    }
}

struct RestaurantCardView: View {
    let card: JSON
    var body: some View {
        let facts = (card["facts"] as? [String]) ?? []
        let buttons = card.a("buttons")
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 8) {
                Text("🍽")
                VStack(alignment: .leading, spacing: 2) {
                    Text(card.s("title")).font(.callout.weight(.semibold)).foregroundColor(.primary)
                    if !card.s("subtitle").isEmpty {
                        Text(card.s("subtitle")).font(.caption).foregroundColor(.secondary)
                    }
                }
            }
            if !facts.isEmpty { Text(facts.joined(separator: " · ")).font(.caption) }
            if !card.s("suggest").isEmpty {
                Text("建议 " + card.s("suggest")).font(.callout.weight(.semibold))
            }
            if !card.s("reason").isEmpty { Text(card.s("reason")).font(.caption).foregroundColor(.secondary) }
            HStack(spacing: 8) {
                ForEach(buttons.indices, id: \.self) { i in
                    let b = buttons[i]
                    if i == 0 {
                        Button { AppLink.open(b) } label: { Text(b.s("label")).font(.callout.weight(.semibold)) }
                            .buttonStyle(.borderedProminent).tint(amber).controlSize(.small)
                    } else {
                        Button { AppLink.open(b) } label: { Text(b.s("label")).font(.callout) }
                            .buttonStyle(.bordered).tint(amber).controlSize(.small)
                    }
                }
            }
            if !card.s("note").isEmpty {
                Text(card.s("note")).font(.caption2).foregroundColor(.secondary)
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

struct FlightCardView: View {
    let card: JSON
    var body: some View {
        let legs = card.a("legs")
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 8) {
                Text("✈️")
                VStack(alignment: .leading, spacing: 2) {
                    Text(card.s("title")).font(.callout.weight(.semibold)).foregroundColor(.primary)
                    Text(card.s("subtitle") + (card.s("cabin").isEmpty ? "" : " · " + card.s("cabin")))
                        .font(.caption).foregroundColor(.secondary)
                }
            }
            ForEach(legs.indices, id: \.self) { li in
                legView(legs[li])
            }
            if !card.s("note").isEmpty {
                Text(card.s("note")).font(.caption2).foregroundColor(.secondary)
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    @ViewBuilder func legView(_ lg: JSON) -> some View {
        let rows = lg.a("rows")
        let plats = lg.a("platforms")
        VStack(alignment: .leading, spacing: 4) {
            Text("\(lg.s("label")) · \(lg.s("from")) → \(lg.s("to"))").font(.caption.weight(.semibold))
            if rows.isEmpty {
                Text("没有查到可比较的价格（点下面的平台直接去查）").font(.caption).foregroundColor(.secondary)
            }
            ForEach(rows.indices, id: \.self) { i in
                let x = rows[i]
                Button { AppLink.open(x.o("link")) } label: {
                    HStack(alignment: .center, spacing: 8) {
                        Text(yuan(num(x, "price"))).font(.callout.weight(.bold).monospacedDigit()).foregroundColor(amber)
                            .frame(width: 64, alignment: .leading)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(x.s("flight_no") + (x.b("transfer") ? " 中转" : "") + " " + x.s("airline"))
                                .font(.caption.weight(.semibold)).foregroundColor(.primary).lineLimit(1)
                            Text("\(x.s("dep_time"))–\(x.s("arr_time")) \(x.s("dep_airport")) \(x.s("arr_airport"))")
                                .font(.caption2).foregroundColor(.secondary).lineLimit(1)
                        }
                        Spacer(minLength: 4)
                        VStack(alignment: .trailing, spacing: 1) {
                            Text(x.s("platform_name")).font(.caption2.weight(.semibold)).foregroundColor(.primary)
                            Text(x.s("tax_note")).font(.caption2).foregroundColor(.secondary).lineLimit(1)
                        }
                    }
                    .padding(.vertical, 4)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("在\(x.s("platform_name"))打开 \(x.s("flight_no")) \(yuan(num(x, "price")))")
                Divider()
            }
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(plats.indices, id: \.self) { i in
                        let p = plats[i]
                        Button { AppLink.open(p.o("link")) } label: {
                            Text(p.s("name") + " · " + p.s("status_cn") + (num(p, "min").map { " " + yuan($0) } ?? ""))
                                .font(.caption2)
                                .padding(.horizontal, 8).padding(.vertical, 4)
                                .background((p.s("status") == "ok" ? okGreen : amber).opacity(0.15))
                                .clipShape(Capsule())
                        }.buttonStyle(.plain)
                    }
                }
            }
        }
    }
}
