# tools

Programas auxiliares que **nao entram no pacote do canal Roku**: o `bsconfig.json` so
empacota `manifest`, `source`, `components`, `images`, `locale` e `settings`, entao esta
pasta fica de fora do zip.

- `frame-server/`: servico que gera os recortes ampliados do modo Explorar. O app Roku
  (`components/video/ExploreOverlay.bs` e `VideoPlayerView.bs`) depende dele. Veja o
  `README.md` da pasta.
