import java.util.Base64

plugins {
    id("com.android.application")
    kotlin("android")
}

val sampleKeystoreBase64 = providers.environmentVariable("ANDROID_SAMPLE_KEYSTORE_BASE64").orNull
val sampleKeystorePassword = providers.environmentVariable("ANDROID_SAMPLE_KEYSTORE_PASSWORD").orNull
val sampleKeyAlias = providers.environmentVariable("ANDROID_SAMPLE_KEY_ALIAS").orNull
val sampleKeyPassword = providers.environmentVariable("ANDROID_SAMPLE_KEY_PASSWORD").orNull
val sampleReleaseKeystore = if (
    !sampleKeystoreBase64.isNullOrBlank() &&
    !sampleKeystorePassword.isNullOrBlank() &&
    !sampleKeyAlias.isNullOrBlank() &&
    !sampleKeyPassword.isNullOrBlank()
) {
    layout.buildDirectory.file("signing/sample-release.jks").get().asFile.apply {
        parentFile.mkdirs()
        writeBytes(Base64.getDecoder().decode(sampleKeystoreBase64))
    }
} else {
    null
}

android {
    namespace = "com.rulemind.sample"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.rulemind.sample"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    signingConfigs {
        create("release") {
            if (sampleReleaseKeystore != null) {
                storeFile = sampleReleaseKeystore
                storePassword = sampleKeystorePassword
                keyAlias = sampleKeyAlias
                keyPassword = sampleKeyPassword
            }
        }
    }

    buildTypes {
        getByName("release") {
            isMinifyEnabled = false
            if (sampleReleaseKeystore != null) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    buildFeatures {
        compose = true
    }

    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.14"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation(project(":rulemind-android"))
    implementation(project(":rulemind-core"))

    // Compose BOM
    val composeBom = platform("androidx.compose:compose-bom:2024.06.00")
    implementation(composeBom)
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material:material-icons-extended")
    debugImplementation("androidx.compose.ui:ui-tooling")
    androidTestImplementation(composeBom)
    androidTestImplementation("androidx.compose.ui:ui-test-junit4")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.6.1")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
    testImplementation("junit:junit:4.13.2")

    implementation("androidx.activity:activity-compose:1.9.0")
    implementation("androidx.lifecycle:lifecycle-viewmodel-ktx:2.8.2")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.2")
    implementation("androidx.navigation:navigation-compose:2.7.7")

    implementation("androidx.core:core-ktx:1.13.1")
}
