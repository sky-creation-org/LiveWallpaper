# Live Wallpaper (Windows)

画像 / GIF / MP4 を壁紙にでき、デジタル時計（月/日/曜日）を表示できる Live 壁紙アプリです。

## ユーザー向け: インストール
`LiveWallpaper-Setup.exe` をダウンロードして実行するだけです（Python 不要・管理者権限不要）。
インストール後はタスクトレイのアイコンから設定を開けます。

## 配布者向け: Setup.exe を自動で作ってオンライン公開する
1. このフォルダを GitHub のリポジトリに push する
2. バージョンタグを付けて push する
   ```
   git tag v1.0.0
   git push origin v1.0.0
   ```
3. GitHub Actions が Windows 上でビルドし、**Releases ページに `LiveWallpaper-Setup.exe` が自動で公開**されます
   （手動実行は Actions タブ → Build installer → Run workflow。成果物は Artifacts に出ます）

## ローカルでビルドする場合
Python 3.10+ と [Inno Setup 6](https://jrsoftware.org/isdl.php) を入れて `build.bat` を実行
→ `installer_output\LiveWallpaper-Setup.exe`

## 補足
- コード署名のない exe は、初回ダウンロード時に Windows SmartScreen の警告が出ることがあります
  （「詳細情報」→「実行」）。警告を避けるにはコード署名証明書での署名が必要です。
- 設定ファイル: `%APPDATA%\LiveWallpaper\config.json`
