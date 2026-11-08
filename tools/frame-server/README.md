# frame-server

Servico de quadros do modo **Explorar** do app Roku. O app Roku nao consegue ampliar o
video 4K sem distorcer a imagem nesta TV, entao ele pede ao servidor um **recorte do quadro
pausado** ja ampliado. O servidor usa o `ffmpeg` do host sobre o arquivo original, que o
Jellyfin indica por `Path`. Ele roda ao lado do Jellyfin, na mesma maquina, e nao e um
plugin do Jellyfin.

## Quem fala com quem
- **Chamadores:** `../../components/video/ExploreOverlay.bs` monta a URL em
  `buildFrameUrl` (porta fixa 8790, mesmo host do Jellyfin) e pede `/clip`.
  `../../components/video/VideoPlayerView.bs` toca essa URL no proprio no de video
  (`onExploreClipRequested`). O caminho `/frame` (JPEG) existe como alternativa
  (`useClipVideo = false` no overlay).
- **Fala com:** o Jellyfin (`--jellyfin`) para validar o token e descobrir o caminho do
  item, e o `ffmpeg`/`ffprobe` do sistema para gerar os quadros.

## Arquivos
- `frame_server.py`: o servidor inteiro (so biblioteca padrao do Python). Pontos de
  entrada: `main()` le os argumentos; `make_handler()` atende `GET /frame` e `GET /clip`.
  Funcoes puras testaveis: `parse_frame_query`, `map_media_path`, `build_ffmpeg_args`,
  `build_clip_args`, `parse_range`, `is_hdr_transfer`, `parse_probe_output`,
  `frame_headers`, `check_clip_output`. `JellyfinClient` isola a chamada ao Jellyfin;
  `probe_video_info` cacheia `ffprobe` por arquivo; `FrameCache` guarda os ultimos 48
  resultados.
- `test_frame_server.py`: testes unitarios das funcoes puras (sem rede, sem `ffmpeg`).

## Contrato HTTP (o que um port, por exemplo para Rust, precisa respeitar)
`GET /frame` e `GET /clip` com a mesma query:

| parametro | regra |
| --- | --- |
| `item` | id do item no Jellyfin, so letras e digitos |
| `t` | segundos, `>= 0` (padrao 0) |
| `x`, `y`, `w`, `h` | recorte em fracao do quadro (0 a 1); `w` e `h` entre 0.02 e 1; o recorte nao sai do quadro |
| `out` | largura de saida em px, 320 a 3840 (padrao 1920) |
| `api_key` | token do usuario no Jellyfin |

- `/frame` devolve `image/jpeg` (BT.709 limitado para cheio, sem HDR). `/clip` devolve
  `video/mp4` de um unico quadro repetido (12 quadros a 24 fps, meio segundo) com
  `Content-Length` e `Range` (206). SDR sai em H.264 8 bits sem converter a cor; HDR
  (`ffprobe` `color_transfer` = `smpte2084` ou `arib-std-b67`) sai em HEVC 10 bits com tag
  `hvc1` e as tags de cor do proprio filme (`-color_primaries`, `-color_trc`,
  `-colorspace bt2020nc`).
- Cabecalhos da resposta (prova de qual quadro foi cortado): `X-Source-Width` e
  `X-Source-Height` (resolucao do arquivo), `X-Crop-Source-Px` (pixels nativos do recorte,
  ex.: `960x540` num zoom 4x de um 4K) e `X-Output-Px` (o que o `scale` gerou).
- Erros: 400 parametro invalido (a mensagem traz o valor e o formato esperado), 401 token
  recusado pelo Jellyfin, 416 `Range` invalido, 404 outro caminho, 502 falha do
  `ffmpeg`/Jellyfin ou clipe vazio (tempo depois do fim do filme).
- Seguranca: o token e validado em `/Users/Me` (cache de 60 s) e o caminho do arquivo so e
  servido se estiver sob um `--map`, sem `..`. Nao ha escrita em disco alem de um
  diretorio temporario por clipe.
- Limites: no maximo 2 `ffmpeg` em paralelo; tempo maximo de 15 s por quadro e 30 s por
  clipe. Um clipe de 0,5 s leva cerca de 0,7 s e pesa ~34 KB. O `ffprobe` (tamanho e cor)
  roda uma vez por arquivo e fica em cache.

## Como usar
Suba no mesmo host do Jellyfin, ligado ao IP da LAN (nao em `0.0.0.0` sem necessidade):

    python3 frame_server.py --jellyfin http://127.0.0.1:8096 \
        --map /media=/caminho/da/midia --bind <ip-do-servidor> --port 8790

- `--map CONTAINER=DISCO` e repetivel e traduz o caminho que o Jellyfin ve (no container)
  para o do disco. Sem ele nenhum arquivo e servido.
- `--eq` anexa um filtro `ffmpeg` so ao JPEG (`/frame`), por exemplo
  `eq=saturation=1.3:contrast=1.1`. O clipe nao usa.
- Testes, dentro desta pasta: `python3 -m unittest test_frame_server`.
- Para trocar a duracao ou a qualidade do clipe, edite `CLIP_FPS`/`CLIP_FRAMES` e os
  parametros de `build_clip_args`.
