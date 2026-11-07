// swift-tools-version:5.9
// 推送解密和钥匙存取：App（mobile/plugin）和通知扩展（mobile/ios/NotificationService）共用。
// 在 Mac 上跑测试：cd mobile/swift/WebPushCrypto && swift test
import PackageDescription

let package = Package(
    name: "WebPushCrypto",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [.library(name: "WebPushCrypto", targets: ["WebPushCrypto"])],
    targets: [
        .target(name: "WebPushCrypto"),
        .testTarget(name: "WebPushCryptoTests", dependencies: ["WebPushCrypto"], resources: [.copy("Fixtures")]),
    ]
)
