import CryptoKit
import Foundation
import Security

/// 这台手机给一台服务器开推送时登记的东西：中转给的地址和 secret、解密用的钥匙。存在钥匙串里，App 和通知扩展共用。
public struct Registration: Codable, Equatable {
    public let id: String         // 中转的地址 id，推送里的 "s" 就是它
    public let secret: String     // 改、删这个登记时要带给中转
    public let endpoint: String   // https://<中转>/w/<id>
    public let origin: String     // 哪台服务器，比如 https://osa.example.com
    public var vapid: String      // 那台服务器的推送公钥
    public var token: String      // 登记时手机的推送令牌
    public var env: String        // production / sandbox
    public let privateKey: Data   // P-256 私钥（32 字节）
    public let auth: Data

    public init(id: String, secret: String, endpoint: String, origin: String, vapid: String, token: String, env: String, keys: PushKeys) {
        self.id = id
        self.secret = secret
        self.endpoint = endpoint
        self.origin = origin
        self.vapid = vapid
        self.token = token
        self.env = env
        self.privateKey = keys.privateKey.rawRepresentation
        self.auth = keys.auth
    }

    public func keys() throws -> PushKeys {
        PushKeys(privateKey: try P256.KeyAgreement.PrivateKey(rawRepresentation: privateKey), auth: auth)
    }

    /// 交给服务器 /api/push/subscribe 的订阅，和浏览器的 PushSubscription.toJSON() 一样
    public func subscription() throws -> [String: Any] {
        ["endpoint": endpoint, "keys": ["p256dh": b64u(try keys().publicKey), "auth": b64u(auth)]]
    }
}

public func b64u(_ data: Data) -> String {
    data.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "=", with: "")
}

public protocol RegistrationStore {
    func all() -> [Registration]
    func get(_ id: String) -> Registration?
    func save(_ r: Registration) throws
    func delete(_ id: String)
}

public extension RegistrationStore {
    func forOrigin(_ origin: String) -> Registration? { all().first { $0.origin == origin } }
}

/// 存在钥匙串里。accessGroup 用 App Group（group.com.kadaliao.opensuperagent），通知扩展才读得到；
/// “首次解锁后、仅本机”：锁屏时扩展也能读，不跟着备份走到别的设备。
public final class KeychainStore: RegistrationStore {
    let service = "com.kadaliao.opensuperagent.push"
    let accessGroup: String?

    public init(accessGroup: String?) {
        self.accessGroup = accessGroup
    }

    private func query(_ extra: [CFString: Any] = [:]) -> CFDictionary {
        var q: [CFString: Any] = [kSecClass: kSecClassGenericPassword, kSecAttrService: service]
        if let g = accessGroup { q[kSecAttrAccessGroup] = g }
        for (k, v) in extra { q[k] = v }
        return q as CFDictionary
    }

    public func all() -> [Registration] {
        var out: CFTypeRef?
        guard SecItemCopyMatching(query([kSecMatchLimit: kSecMatchLimitAll, kSecReturnAttributes: true]), &out) == errSecSuccess,
              let items = out as? [[String: Any]] else { return [] }
        return items.compactMap { ($0[kSecAttrAccount as String] as? String).flatMap(get) }
    }

    public func get(_ id: String) -> Registration? {
        var out: CFTypeRef?
        guard SecItemCopyMatching(query([kSecAttrAccount: id, kSecReturnData: true]), &out) == errSecSuccess,
              let data = out as? Data else { return nil }
        return try? JSONDecoder().decode(Registration.self, from: data)
    }

    public func save(_ r: Registration) throws {
        let data = try JSONEncoder().encode(r)
        SecItemDelete(query([kSecAttrAccount: r.id]))
        let status = SecItemAdd(query([kSecAttrAccount: r.id, kSecValueData: data,
                                       kSecAttrAccessible: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]), nil)
        guard status == errSecSuccess else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
    }

    public func delete(_ id: String) {
        SecItemDelete(query([kSecAttrAccount: id]))
    }
}

/// 测试用
public final class MemoryStore: RegistrationStore {
    var items: [String: Registration] = [:]
    public init() {}
    public func all() -> [Registration] { items.values.sorted { $0.id < $1.id } }
    public func get(_ id: String) -> Registration? { items[id] }
    public func save(_ r: Registration) throws { items[r.id] = r }
    public func delete(_ id: String) { items[id] = nil }
}
