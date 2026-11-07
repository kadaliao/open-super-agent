import CryptoKit
import Foundation
import XCTest
@testable import WebPushCrypto

func unb64u(_ s: String) -> Data {
    var t = s.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
    while t.count % 4 != 0 { t += "=" }
    return Data(base64Encoded: t)!
}

final class WebPushCryptoTests: XCTestCase {
    /// RFC 8291 附录 A 的例子
    func testRfc8291Example() throws {
        let keys = PushKeys(privateKey: try P256.KeyAgreement.PrivateKey(rawRepresentation: unb64u("q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94")),
                            auth: unb64u("BTBZMqHH6r4Tts7J_aSIgg"))
        XCTAssertEqual(b64u(keys.publicKey), "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4")
        let body = unb64u("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")
        let plain = try Aes128gcm.decrypt(body, keys: keys)
        XCTAssertEqual(String(data: plain, encoding: .utf8), "When I grow up, I want to be a watermelon")
    }

    /// 服务器（osa/push.py）真的加密出来的一条，见 mobile/scripts/make-push-fixture.py
    func testDecryptsWhatTheServerSends() throws {
        let (keys, body, want) = try fixture()
        let payload = PushPayload.parse(try Aes128gcm.decrypt(body, keys: keys))
        XCTAssertEqual(payload, PushPayload(title: want["title"] as! String, body: want["body"] as! String,
                                            url: "/?chat=12&thread=34", tag: "approval-7", badge: 2))
    }

    func testTamperedOrWrongKeysFail() throws {
        let (keys, body, _) = try fixture()
        var bad = body
        bad[bad.count - 1] ^= 1
        XCTAssertThrowsError(try Aes128gcm.decrypt(bad, keys: keys)) { XCTAssertEqual($0 as? WebPushError, .decryptFailed) }
        XCTAssertThrowsError(try Aes128gcm.decrypt(body, keys: PushKeys.generate())) { XCTAssertEqual($0 as? WebPushError, .decryptFailed) }
        XCTAssertThrowsError(try Aes128gcm.decrypt(Data(repeating: 1, count: 50), keys: keys)) { XCTAssertEqual($0 as? WebPushError, .malformed) }
        var notAPoint = body
        notAPoint[30] ^= 0xff
        XCTAssertThrowsError(try Aes128gcm.decrypt(notAPoint, keys: keys))
    }

    func testOnlySameSiteUrls() {
        for (given, want) in [("/?chat=1", "/?chat=1"), ("//evil.example/x", "/"), ("https://evil.example", "/"), ("javascript:alert(1)", "/"), ("/\\evil", "/")] {
            XCTAssertEqual(PushPayload.parse(Data("{\"url\": \"\(given.replacingOccurrences(of: "\\", with: "\\\\"))\"}".utf8))?.url, want, given)
        }
        XCTAssertNil(PushPayload.parse(Data("not json".utf8)))
        XCTAssertNil(PushPayload.parse(Data("{\"title\": \"x\"}".utf8))?.badge)
    }

    func testRegistrationGivesASubscription() throws {
        let keys = PushKeys.generate()
        XCTAssertEqual(keys.publicKey.count, 65)
        XCTAssertEqual(keys.auth.count, 16)
        let r = Registration(id: "abc", secret: "s", endpoint: "https://push.example/w/abc", origin: "https://osa.example.com",
                             vapid: "k", token: "ab", env: "production", keys: keys)
        let sub = try r.subscription()
        XCTAssertEqual(sub["endpoint"] as? String, "https://push.example/w/abc")
        let k = sub["keys"] as! [String: String]
        XCTAssertEqual(unb64u(k["p256dh"]!), keys.publicKey)
        XCTAssertFalse(k["auth"]!.contains("="))
        XCTAssertEqual(try r.keys().privateKey.rawRepresentation, keys.privateKey.rawRepresentation)

        let store = MemoryStore()
        try store.save(r)
        XCTAssertEqual(store.forOrigin("https://osa.example.com"), r)
        XCTAssertNil(store.forOrigin("https://other.example"))
        let roundTrip = try JSONDecoder().decode(Registration.self, from: JSONEncoder().encode(r))
        XCTAssertEqual(roundTrip, r)
    }

    private func fixture() throws -> (PushKeys, Data, [String: Any]) {
        let url = Bundle.module.url(forResource: "from-server", withExtension: "json", subdirectory: "Fixtures")!
        let f = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        let keys = PushKeys(privateKey: try P256.KeyAgreement.PrivateKey(rawRepresentation: unb64u(f["private_key"] as! String)),
                            auth: unb64u(f["auth"] as! String))
        return (keys, unb64u(f["body"] as! String), f["payload"] as! [String: Any])
    }
}
