import Foundation

/// 服务器推送的内容（osa/push.py 的 send）：标题、正文、点开打开的站内地址、合并用的 tag、待确认数。
public struct PushPayload: Equatable {
    public let title: String
    public let body: String
    public let url: String
    public let tag: String
    public let badge: Int?

    public init(title: String, body: String, url: String, tag: String, badge: Int?) {
        self.title = title
        self.body = body
        self.url = url
        self.tag = tag
        self.badge = badge
    }

    /// 解析解密后的 JSON。地址只接受站内的（以 / 开头、不是 //），别的一律当成 /。
    public static func parse(_ data: Data) -> PushPayload? {
        guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return nil }
        let url = obj["url"] as? String ?? "/"
        return PushPayload(
            title: String((obj["title"] as? String ?? "").prefix(120)),
            body: String((obj["body"] as? String ?? "").prefix(400)),
            url: safePath(url),
            tag: obj["tag"] as? String ?? "",
            badge: (obj["badge"] as? NSNumber)?.intValue
        )
    }

    public static func safePath(_ url: String) -> String {
        url.hasPrefix("/") && !url.hasPrefix("//") && !url.contains("\\") ? url : "/"
    }
}
