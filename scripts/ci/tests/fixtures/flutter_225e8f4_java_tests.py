# Copyright 2013 The Flutter Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found at https://github.com/flutter/flutter/blob/master/LICENSE.
#
# Unmodified Java test entrypoint from the failing run's target ref:
# https://github.com/littleGnAl/flutter/blob/225e8f4e687e34a0f5c21c3f56d90e934b5ced76/engine/src/flutter/testing/run_tests.py#L718-L757
# The regression test supplies its platform helpers and command executor.

import os

def java_home():
  script_path = os.path.dirname(os.path.realpath(__file__))
  if is_mac():
    return os.path.join(script_path, '..', 'third_party', 'java', 'openjdk', 'Contents', 'Home')
  return os.path.join(script_path, '..', 'third_party', 'java', 'openjdk')


def java_bin():
  return os.path.join(java_home(), 'bin', 'java.exe' if is_windows() else 'java')


def run_java_tests(executable_filter, android_variant='android_debug_unopt'):
  """Runs the Java JUnit unit tests for the Android embedding"""
  test_runner_dir = os.path.join(
      BUILDROOT_DIR, 'flutter', 'shell', 'platform', 'android', 'test_runner'
  )
  gradle_bin = os.path.join(
      BUILDROOT_DIR, 'flutter', 'third_party', 'gradle', 'bin',
      'gradle.bat' if is_windows() else 'gradle'
  )
  flutter_jar = os.path.join(OUT_DIR, android_variant, 'flutter.jar')
  android_home = os.path.join(BUILDROOT_DIR, 'flutter', 'third_party', 'android_tools', 'sdk')
  build_dir = os.path.join(OUT_DIR, android_variant, 'robolectric_tests', 'build')
  gradle_cache_dir = os.path.join(OUT_DIR, android_variant, 'robolectric_tests', '.gradle')

  test_class = executable_filter if executable_filter else '*'
  command = [
      gradle_bin,
      '-Pflutter_jar=%s' % flutter_jar,
      '-Pbuild_dir=%s' % build_dir,
      'testDebugUnitTest',
      '--tests=%s' % test_class,
      '--rerun-tasks',
      '--no-daemon',
      '--project-cache-dir=%s' % gradle_cache_dir,
      '--gradle-user-home=%s' % gradle_cache_dir,
  ]

  env = dict(os.environ, ANDROID_HOME=android_home, JAVA_HOME=java_home())
  run_cmd(command, cwd=test_runner_dir, env=env)
