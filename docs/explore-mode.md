# Modo Explorar e demais mudancas deste fork

Este fork do `jellyfin-roku` (3.2.5) foi ajustado para uma Roku TV 4K (SEMP 50RK8500,
Roku OS 15.3.4). O que foi medido na TV vem dito como fato; palpite leva "hipotese".
Os commits estao na branch `staging`; nada foi enviado ao repositorio oficial.

## O que mudou
- **Tema bioluminescente:** cores em `source/enums/ColorPalette.bs`, fundos animados
  `AuroraBackground`, `AuroraSonar` e `JellyfishBackground` (aguas-vivas com sprites em
  `images/brand/`), telas de servidor e login, botoes em pilula e foco dos detalhes de
  filme.
- **Home com hero:** ao focar um item aparece o backdrop com titulo e sinopse
  (`components/home/Home.bs`, `HeroDetailsTask`, espera de 0,4 s e cache).
- **Barra de buffer** no player (`components/video/OSD`, `VideoPlayerView.bs`): soma a
  duracao dos segmentos baixados; em MP4 progressivo a Roku nao informa inicio nem nivel.
- **Modo Explorar:** com o filme pausado, amplia o quadro com mira ou quadrantes.

## Modo Explorar
Botao `exploreMode` na ultima posicao das opcoes do player. Teclas: canal+ ou avancar
aproxima; canal- ou voltar rapido afasta; Replay alterna mira e quadrantes; setas movem
(nas bordas deslocam o recorte); OK nos quadrantes amplia; **OK na mira mostra um QR para
baixar aquele quadro no celular**; Voltar zera o zoom e depois sai; Play sai e retoma o
filme. Zooms: 1, 1.5, 2, 3, 4, 6 e 8x (`source/utils/exploreMath.bs`).

### Como funciona
O app nao amplia o video da TV. Ele pede ao `tools/frame-server` um **clipe MP4 de um
quadro** ja recortado (3840 px de largura) e toca no proprio no de video do player,
pausado no primeiro quadro. O filme e a posicao ficam guardados e voltam ao sair.
- `components/video/ExploreOverlay.bs/.xml`: estado, teclas e interface. O rodape mostra
  `Zoom Nx - fonte LxA px`: os pixels **nativos** que aquele recorte tem (um zoom 4x de
  um quadro 4K mostra so 960x540 de origem, esticados). O palco usa a proporcao real do
  filme (`streamInfo` do player), nao 16:9 fixo.
- `components/video/VideoPlayerView.bs`: troca e restaura o conteudo; durante o Explorar
  ignora estado, posicao, buffer e relatorio de reproducao.
- `tools/frame-server/`: o servico (contrato HTTP no README da pasta). HDR (PQ ou HLG)
  sai em HEVC 10 bits com as tags de cor do filme; SDR em H.264 sem converter a cor. A
  resposta traz `X-Crop-Source-Px` e `X-Source-Width` para conferir o recorte por `curl`.
- A constante `useClipVideo` no overlay volta ao JPEG em `Poster` quando e `false`.

### Por que assim
1. Redimensionar ou escalar o no de video distorce a imagem nesta TV.
2. JPEG em `Poster` fica com cor diferente: a TV aplica o processamento de imagem
   (saturacao e contraste) so ao plano de video.
3. Um segundo no `Video` e recusado ("only one play instance allowed"); por isso o clipe
   reaproveita o no do player.
4. Um recorte 4x de um quadro 4K tem cerca de 960 px de origem esticados: fica macio
   mesmo no video nativo.

## Limites da TV (medidos)
- Chegam ao app: setas, OK, voltar, play, avancar, voltar rapido, replay, canal+ e canal-.
- Nao chegam: volume e `*` (a TV abre o proprio menu de imagem).
- A interface renderiza no maximo ~48 fps.
- O print do instalador nao captura a camada de video.

## Compilar, instalar e testar
1. `npm run build` (gera `build/staging`).
2. Enviar para a TV em modo desenvolvedor (instalador em `http://<ip-da-tv>`, usuario
   `rokudev`).
3. Subir o servico de quadros ao lado do Jellyfin; exemplo no `README.md` de
   `tools/frame-server`. Sem ele, o Explorar mostra erro.
4. Testes do servico: `python3 -m unittest test_frame_server` dentro de `tools/frame-server`.

## Pendente
- Tempo de troca de zoom: o encode caiu de 2,3 s (96 quadros) para 0,7 s (12 quadros) num
  4K HDR10 medido com `ffmpeg` no servidor; falta confirmar o tempo total na TV.
- Cor e nitidez em SDR com o clipe ainda nao foram confirmadas pelo usuario (so HDR).
- HDR10+ e Dolby Vision nao sao tratados (so PQ e HLG por tag).
- `hevc_vaapi` foi medido e nao ganhou nada com 12 quadros (0,74 s contra 0,74 s do
  `libx265`), entao o caminho por hardware ficou de fora. O servico e um script Python;
  um port para Rust so vale se o encode deixar de ser o gargalo.
