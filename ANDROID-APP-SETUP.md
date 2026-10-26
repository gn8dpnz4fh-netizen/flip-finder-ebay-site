# Flip Finder Capacitor setup

This branch now contains an initial Capacitor scaffold and Android-friendly input fixes. It does **not** yet contain a built APK/IPA or store-ready native app.

## Setup on a computer

1. Install a current Node.js LTS release.
2. Download/clone this branch: `android-native-app-prep`.
3. In this repository folder run:

   ```bash
   npm install
   npm run prepare:native
   npx cap add android
   npx cap sync android
   npx cap open android
   ```

4. Build and test on a real Android device using Android Studio. For iOS, a Mac with Xcode is required; run `npx cap add ios`, `npx cap sync ios`, then `npx cap open ios`.

## Important limitations before release

- The current app calls the existing hosted backend at `https://flipfinder-ebay-notifications.onrender.com`; keep that service healthy.
- The existing web camera uses `getUserMedia`. Verify Android camera permission behavior on a real device before considering it fixed. A native camera plugin/permission bridge may be required.
- Verify Supabase login, Stripe checkout, and return-to-app behavior on Android and iOS. Native deep links and store billing requirements may require separate setup.
- Do not commit server secrets or private Stripe keys. The Supabase publishable key in the website is intended for client use; secure access through Supabase policies and server-side validation.
- Do not merge this branch into `main` or deploy it before the current iPhone experience and Android flows pass testing.

The `prepare:native` script copies the existing website files to `www` and removes service-worker registration only from that packaged copy. It does not change the hosted website.
