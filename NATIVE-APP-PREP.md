# Flip Finder — Native App Preparation

## Branch safety
- Working branch: `android-native-app-prep`
- Based on: `android-native-compatibility`
- The live `main` branch has not been changed.
- Do not merge this branch into `main` or deploy it until the Android and iPhone flows have been tested.

## Goal
Prepare Flip Finder for Android and iPhone while preserving the currently working iPhone/web experience. The current project is a web app/PWA, not yet a complete native Android/iOS project.

## Before changing code
1. Keep the current `main` branch as the known-good backup.
2. Reproduce and record each Android issue in the browser where it occurs.
3. Check search typing, camera permission/start, item research, Supabase sign-in, saved flips, Stripe checkout/trial return, and navigation.
4. Do not commit Supabase service-role keys, Stripe secret keys, or other private secrets. Public client keys may be used only where designed for browser exposure and protected by proper backend policies.

## Native app approach
Use Capacitor only after the web app's mobile behavior is verified. Do not treat a thin remote-website wrapper as a finished store-ready app. The native builds need proper camera/permission handling, app lifecycle behavior, external checkout/link handling, and device testing.

## Android test checklist
- [ ] Confirm browser and Android version.
- [ ] Search field accepts typing and keyboard does not cover the field.
- [ ] Camera permission prompt appears correctly and scan works after permission is granted.
- [ ] Denying camera permission does not freeze the app; manual text search still works.
- [ ] Sign-in and sign-out work.
- [ ] Scan limits and Pro trial/checkout return to the app correctly.
- [ ] Saved flips and profile behavior are clear.
- [ ] No stale service-worker cache serves old code.

## iPhone regression checklist
- [ ] Existing iPhone experience still loads and navigates.
- [ ] Camera and manual search work.
- [ ] Supabase authentication still works.
- [ ] Pro checkout/trial return still works.
- [ ] No changes are deployed to production until checks pass.

## First working session
When resuming, start by identifying the Android browser (Chrome, Samsung Internet, Firefox, or other), Android version, and the exact broken behavior. Fix and test the web app on this branch first, then add and configure Capacitor for native builds. Store signing and publishing are separate final steps.
