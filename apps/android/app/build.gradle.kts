import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// One version for iOS and Android: apps/android/version.properties (kept equal to apps/ios/Info.plist)
val ver = Properties().apply { rootProject.file("version.properties").inputStream().use { load(it) } }

android {
    namespace = "com.hackjown.aiw"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.hackjown.aiworkstation"   // same id as the iOS app
        minSdk = 26
        targetSdk = 34
        versionCode = ver.getProperty("versionCode").trim().toInt()
        versionName = ver.getProperty("versionName").trim()
    }

    // Sideload key, committed on purpose (personal use): every CI build has the same signature, so a new APK
    // installs over the old one and keeps the settings. It is NOT a secret and NOT for the Play Store.
    signingConfigs {
        create("sideload") {
            storeFile = file("aiw-sideload.p12")
            storeType = "pkcs12"
            storePassword = "aiw-sideload"
            keyAlias = "aiw"
            keyPassword = "aiw-sideload"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("sideload")
        }
        debug {
            signingConfig = signingConfigs.getByName("sideload")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    buildFeatures { buildConfig = true }

    lint {
        abortOnError = false
        checkReleaseBuilds = false
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.1")
    implementation("androidx.swiperefreshlayout:swiperefreshlayout:1.1.0")
    implementation("androidx.webkit:webkit:1.11.0")
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
}
