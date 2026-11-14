plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "br.mark.assistant"
    compileSdk = 34

    defaultConfig {
        applicationId = "br.mark.assistant"
        minSdk = 26
        targetSdk = 34
        versionCode = 19
        versionName = "1.9.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // Signed with the debug key so the APK installs directly; this is a
            // personal app, not a Play Store release.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // MediaSession / MediaBrowserService: Android Auto, lock screen, headset buttons
    implementation("androidx.media:media:1.7.0")
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    // Google's scanner (Play services): continuous autofocus and auto-zoom, much
    // better at reading a QR code off a monitor. ZXing stays as the fallback.
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")
}
