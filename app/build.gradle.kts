plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.ksp)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.secrets)
    alias(libs.plugins.ktlint)
}

android {
    // Le paquet des sources Kotlin, aligné sur l'identité de l'application
    // (`com.aitrade.app`) : le code vit dans le même espace de noms que la
    // l'application qu'il produit. Les deux restent distincts — `applicationId`
    // est l'identité *du système*, `namespace` celui du *code* (R, BuildConfig).
    namespace = "com.aitrade"
    compileSdk = 36

    defaultConfig {
        // L'identité de l'application, telle que le système et les magasins la
        // connaissent. Elle a été reprise à celle du gabarit d'export ; c'est
        // désormais l'identité **du projet**, et elle ne se change plus après
        // publication : la changer, c'est publier une autre application (voir
        // README, « L'identité de l'application Android »).
        //
        // `namespace` (juste au-dessus) reste le paquet des sources Kotlin, pas
        // l'identité de l'application : le renommer demande de déplacer les
        // sources, et n'a **aucun** effet sur une installation existante. Les
        // deux ne doivent pas être confondus — c'est ce que l'export faisait.
        applicationId = "com.aitrade.app"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    signingConfigs {
        // Le keystore de debug n'est **pas** versionné (`.gitignore` exclut
        // `*.keystore`) : le référencer sans condition casse donc toute
        // construction sur un clone neuf, CI comprise (c'est le cas ici : le
        // fichier est absent). AGP fournit déjà une signature de debug par
        // défaut (`~/.android/debug.keystore`, généré à la demande) ; cette
        // surcharge n'est donc utilisée que si le fichier existe réellement.
        val debugKeystore = file("$rootDir/debug.keystore")
        if (debugKeystore.exists()) {
            create("debugConfig") {
                storeFile = debugKeystore
                storePassword = "android"
                keyAlias = "androiddebugkey"
                keyPassword = "android"
            }
        }
    }

    buildTypes {
        debug {
            // Idem : on ne référence la surcharge que lorsqu'elle a été créée.
            signingConfigs.findByName("debugConfig")?.let { signingConfig = it }
        }
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_21
        targetCompatibility = JavaVersion.VERSION_21
    }
    kotlinOptions {
        jvmTarget = "21"
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.14"
    }

    testOptions {
        unitTests {
            // Requis par Robolectric : les tests Compose UI tournent sur la JVM
            // (src/test) sans émulateur, mais ont besoin des ressources Android.
            isIncludeAndroidResources = true
        }
    }
}

secrets {
    propertiesFileName = ".env"
    defaultPropertiesFileName = ".env.example"
}

ktlint {
    // Moteur épinglé : le ruleset ne doit pas changer d'une machine à l'autre.
    version.set(libs.versions.ktlint.get())

    // GATE BLOQUANT, et le seul : `./gradlew :app:ktlintCheck` échoue sur toute
    // violation. Les sources ont été formatées une fois avec ce même moteur
    // (`./gradlew :app:ktlintFormat`), donc le gate part d'un état propre qu'il
    // ne fait que défendre.
    //
    // Le ruleset complet s'applique, à une exception près : `no-wildcard-imports`
    // est désactivée dans `.editorconfig`, car ce projet conserve des imports
    // étoile volontaires (Compose / engine / theme). On ne peut PAS la désactiver
    // ici : l'option `disabledRules` n'est plus supportée par ktlint-gradle
    // depuis le moteur ktlint 0.48.
    ignoreFailures.set(false)
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.activity.compose)

    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.material.icons.extended)

    implementation(libs.androidx.room.runtime)
    implementation(libs.androidx.room.ktx)
    ksp(libs.androidx.room.compiler)

    implementation(libs.retrofit)
    implementation(libs.retrofit.converter.serialization)
    implementation(libs.okhttp)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.kotlinx.coroutines.android)

    // --- Tests Compose UI (JVM / Robolectric, aucun device requis) ---
    testImplementation(libs.junit)
    testImplementation(platform(libs.androidx.compose.bom))
    testImplementation(libs.androidx.compose.ui.test.junit4)
    testImplementation(libs.androidx.test.core)
    testImplementation(libs.androidx.test.ext.junit)
    testImplementation(libs.robolectric)
    debugImplementation(libs.androidx.compose.ui.test.manifest)
}
