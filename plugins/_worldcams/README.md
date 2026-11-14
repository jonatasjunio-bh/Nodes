# World Cams — câmeras ao vivo do mundo no HUD

Plugin do MARK LV (Jarvis) que mostra **câmeras públicas ao vivo** de qualquer lugar do mundo, direto no HUD, onde normalmente fica o avatar.

Há duas formas de usar:

1. **Mapa-múndi navegável:** as câmeras ao vivo aparecem **no lugar delas no mapa**. De longe, ficam agrupadas com a **quantidade** em cada região; ao aproximar, os grupos se abrem até cada câmera ter seu pino. Passar o mouse num pino mostra a miniatura e o título; clicar toca a câmera no HUD, numa **janela menor** sobre o mapa. Também dá para escolher continente → país → cidade, e o painel ao lado lista as câmeras do lugar.
2. **Direto por voz:** "mostra uma câmera ao vivo de Tóquio" abre a câmera sem passar pelo mapa.

---

## Sumário

- [Como usar](#como-usar)
- [Instalação](#instalação)
- [De onde vêm as câmeras](#de-onde-vêm-as-câmeras)
- [Arquivos](#arquivos)
- [Como funciona por dentro](#como-funciona-por-dentro)
- [Alterações no `ui.py`](#alterações-no-uipy)
- [Problemas encontrados e soluções](#problemas-encontrados-e-soluções)
- [Limitações](#limitações)
- [O que foi testado](#o-que-foi-testado)
- [Como desfazer](#como-desfazer)

---

## Como usar

### Comandos de voz

| Você diz | Ação do plugin | O que acontece |
|---|---|---|
| "abre o mapa de câmeras" | `map` | Abre o mapa-múndi no HUD |
| "mapa de câmeras do Japão" / "da Europa" | `map` + `place` | Abre o mapa já com zoom no lugar |
| "fecha o mapa" | `close_map` | Fecha o mapa e volta ao avatar |
| "mostra uma câmera ao vivo de Paris" | `watch` | Toca direto a primeira câmera ao vivo do lugar |
| "quero ver a praia de Copacabana agora" | `watch` | Idem |
| "outra câmera" | `next` | Passa para a próxima câmera do mesmo lugar |
| "me leva pra algum lugar do mundo" | `random` | Escolhe um lugar famoso ao acaso |
| "quais câmeras tem em Roma?" | `list` | O Jarvis lê os títulos das câmeras |
| "a terceira" | `pick` (`number=3`) | Abre a câmera escolhida da lista |
| "fecha o vídeo" / "para o vídeo" | (`video_player`) | Fecha a câmera: se o mapa estava aberto, volta para ele |

> "Outra câmera" funciona tanto depois de um comando de voz quanto depois de uma busca feita no mapa.

### Navegação no mapa

- **Mundo:** passe o mouse sobre um continente para destacá-lo, e clique nele ou use os botões na parte de baixo (África, Europa, Ásia…).
- **Continente:** clique num país. O mapa dá zoom no país, e o painel já lista câmeras do país inteiro.
- **País:** clique numa cidade (ou em qualquer ponto). O lugar é identificado, marcado com um círculo laranja, o mapa dá zoom, e o painel lista as câmeras de lá.
- **Ir para…:** a caixa no topo aceita cidade, praia, ponto turístico, país ou continente ("Copacabana", "Shibuya", "França", "Europa").
- **Navegação no topo** (`MUNDO › ÁSIA › JAPÃO › Osaka`): clique num nível para voltar a ele.
- **Esc:** sobe um nível (lugar → país → continente → mundo).
- **↻ Procurar de novo:** refaz a busca do lugar atual.
- **✕ CLOSE** (cabeçalho do HUD): fecha o mapa.

As câmeras sempre abrem **sem som**: o stream ao vivo do YouTube nesse formato só traz imagem.

---

## Instalação

Dependências (já instaladas nesta máquina):

```bash
python -m pip install yt-dlp
```

```bash
python -m pip install "PyQt6-WebEngine==6.11.0"
```

- **yt-dlp**: obrigatório. Busca as lives e resolve o endereço do stream.
- **PyQt6-WebEngine**: necessário para o mapa aparecer **dentro do HUD**. A versão precisa bater com a do PyQt6 (PyQt6 6.11 → PyQt6-WebEngine 6.11). Sem ele, o mapa abre no navegador padrão, e as câmeras continuam tocando no HUD.

Os dois estão no `requirements.txt`. Depois de instalar, basta reiniciar o Jarvis: o plugin é descoberto sozinho na inicialização.

É preciso ter internet: o mapa de fundo, a identificação dos lugares e as câmeras vêm de serviços online.

---

## De onde vêm as câmeras

| Fonte | Chave? | Posição no mapa | Tipo |
|---|---|---|---|
| **YouTube Live** | Não | Sim, no lugar que a câmera mostra (veja abaixo) | Vídeo ao vivo de verdade |

**YouTube:** a busca usa o filtro "ao vivo agora" do próprio YouTube, então gravações antigas não aparecem. Títulos com "cam", "câmera", "camera", "ライブカメラ" etc. vão para o topo, porque a busca também traz noticiário ao vivo.

**Como as câmeras do YouTube vão para o mapa:** uma live não informa coordenadas. Em segundo plano, o plugin busca cerca de 115 lugares conhecidos do mundo (4 buscas por vez, ~45 s) e marca uma câmera num lugar **só se o título dela citar esse lugar**. Na busca por Copacabana vem também uma câmera de Cabo Frio, que não pode ser marcada em Copacabana. Cada câmera fica um pouco afastada do centro do lugar, sempre na mesma posição, para os pinos não se sobreporem. Lugares que você clica no mapa acrescentam as câmeras deles do mesmo jeito. O resultado fica guardado por 12 horas em `memory/worldcams_pins.json`; ao atualizar, saem as câmeras que não estão mais ao vivo. O contador no topo mostra o total e o progresso ("mapeando 45/116").

**Windy Webcams: testada e retirada.** Com uma chave de verdade: 69 mil câmeras no mundo, mas quase todas na Europa, só 151 no Brasil e nenhuma na cidade do Rio; e a maioria é foto com timelapse, não vídeo ao vivo (8 de 50 no Brasil). Não compensou.

**Fontes excluídas de propósito:** câmeras que só estão acessíveis porque ficaram sem senha (Insecam, buscas no Shodan etc.). Elas mostram casas e lojas cujos donos não querem ser vistos. O plugin só usa transmissões publicadas intencionalmente.

**Outros serviços usados:**

| Serviço | Para quê |
|---|---|
| Esri World Dark Gray (Base + Reference) | Mapa de fundo e rótulos, sem chave |
| Natural Earth 110m (via jsDelivr) | Contornos dos países, nome em português e continente |
| OpenStreetMap Nominatim | Identificar a cidade clicada e a busca "Ir para…" |
| Leaflet 1.9.4 (via cdnjs) | Biblioteca do mapa |
| Leaflet.markercluster 1.5.3 (via cdnjs) | Agrupar as câmeras com a quantidade quando o zoom está longe |

O Nominatim é chamado **no máximo uma vez por segundo**, como pede a política de uso dele, com um User-Agent identificado.

---

## Arquivos

```
Mark-LV-main/ (e Mark-LV-ollama/)
├── plugins/
│   ├── world_cams.py              ← o plugin (ações de voz + servidor local do mapa)
│   └── _worldcams/
│       ├── map.html               ← a página do mapa
│       └── README.md              ← este documento
├── memory/
│   ├── worldcams_countries.geojson ← cache dos países (criado no primeiro uso)
│   └── worldcams_pins.json        ← câmeras no mapa (atualizado a cada 12 h)
├── ui.py                          ← ganhou a "tela web" do HUD
└── requirements.txt               ← + PyQt6-WebEngine
```

A pasta `_worldcams` começa com `_` de propósito: o carregador de plugins ignora arquivos com esse prefixo, então ela não é confundida com um plugin.

---

## Como funciona por dentro

```
 Voz ──► Gemini ──► world_cams.run(action="map")
                         │
                         ├─ sobe o servidor local (127.0.0.1, porta aleatória)
                         └─ player.show_web(url) ──► HUD mostra o mapa (QWebEngineView)
                                                          │
       clique na cidade ──► /api/reverse ──► Nominatim ───┤
       lista do painel  ──► /api/cams    ──► YouTube ─────┤
       clique na câmera ──► /api/play    ──► yt-dlp ──► player.show_video(HLS)
                                                          │
                                       HUD toca a live; ao fechar, volta ao mapa
```

### O servidor local

O plugin sobe um `ThreadingHTTPServer` da biblioteca padrão do Python, na primeira vez em que o mapa é aberto:

- Escuta **só em `127.0.0.1`**, numa porta aleatória.
- Toda chamada `/api/*` exige um **código (token) gerado a cada vez que o Jarvis abre**. Sem ele, a resposta é `403`. Assim, nenhum site aberto no navegador consegue mandar o Jarvis tocar algo.

| Rota | Função |
|---|---|
| `GET /` | A página `map.html` |
| `GET /countries.geojson` | Países (baixados uma vez e guardados em `memory/`) |
| `GET /api/cams?q=…&label=…` | Lives do YouTube para o lugar |
| `GET /api/reverse?lat=…&lon=…` | Nome da cidade num ponto (Nominatim) |
| `GET /api/geocode?q=…` | Localiza um nome digitado (Nominatim) |
| `GET /api/play?id=…&title=…` | Resolve a live e manda tocar no HUD |
| `GET /api/pins` | Todas as câmeras com posição, e o progresso do mapeamento |
| `GET /api/rebuild` | Refaz o mapeamento agora |

### Por que o plugin resolve o stream sozinho

O `video_player` do Jarvis entrega tudo ao yt-dlp e só aceita formatos HTTP comuns. Uma live do YouTube só oferece **HLS (m3u8)**, então pelo `video_player` toda câmera falharia com "no playable stream". O plugin escolhe o formato HLS de até 720p e o passa direto ao player do HUD; o backend FFmpeg do Qt toca HLS nativamente. A primeira imagem leva cerca de 3 s.

Fechar, silenciar e cancelar continuam sendo do `video_player`: o plugin usa o mesmo contador de pedidos (`_begin_open` / `_still_wanted`). Por isso "fecha o vídeo" funciona numa câmera como num vídeo comum, inclusive se ela ainda estiver carregando.

---

## Alterações no `ui.py`

O HUD tinha três "telas" no mesmo lugar (avatar, câmera do PC e vídeo). Agora tem quatro:

| Índice | Tela |
|---|---|
| 0 | Avatar (OpenGL) |
| 1 | Câmera do computador |
| 2 | Vídeo |
| **3** | **Página web (mapa)** ← nova |

O que foi adicionado:

- **Import opcional** de `QWebEngineView` (`HAVE_WEB`). Se o pacote não existir, o Jarvis abre normalmente e só avisa no console.
- `os.environ.setdefault("QSG_RHI_BACKEND", "opengl")` antes desse import (veja [Problemas](#problemas-encontrados-e-soluções)).
- **Sinais** `_web_open_sig(url, title)` e `_web_close_sig()`, para outros threads poderem abrir e fechar a página com segurança.
- **Tela web** com cabeçalho (título + `✕ CLOSE`) no mesmo estilo da tela de vídeo.
- `_on_web_open`: abrir o mesmo endereço de novo só mostra a página, sem recarregar, então o mapa mantém o zoom.
- `_on_web_close`: carrega `about:blank` para parar os processos da página.
- **Fechar o vídeo** (e parar a câmera do PC) volta para a tela 3 se o mapa estiver aberto, e para a 0 se não estiver.
- **Novos métodos em `JarvisUI`**, que os plugins podem usar: `web_available()`, `show_web(url, title)`, `close_web()` e `web_is_open()`.

Essa tela é genérica: qualquer outro plugin pode exibir uma página no HUD com `player.show_web(...)`.

---

## Problemas encontrados e soluções

| Problema | Causa | Solução |
|---|---|---|
| Jarvis caía (*access violation*) ao voltar do mapa para o avatar | O WebEngine desenha com Direct3D no Windows; o avatar é um `QOpenGLWidget` na mesma janela | `QSG_RHI_BACKEND=opengl` antes de importar o WebEngine |
| Mapa de fundo mostrava "API KEY REQUIRED" | O CARTO passou a exigir chave | Troca para o Esri World Dark Gray, que não pede chave |
| Erro `Invalid LatLng (NaN, NaN)` | Bug do Leaflet: `flyTo` para o lugar onde o mapa já está divide por zero | Funções `fly()`/`flyPt()` que ignoram voos para o mesmo lugar |
| Nomes de cidades duplicados em dois tamanhos | O Leaflet só descarta os tiles do zoom anterior quando o fade termina | `fadeAnimation: false` |
| Contorno amarelo no país clicado | Contorno de foco do navegador | `.leaflet-interactive:focus { outline: none }` |
| Noticiário ao vivo no meio das câmeras | O filtro "ao vivo" do YouTube pega qualquer live | Títulos com "cam"/"câmera" vão para o topo |
| Câmera nunca tocava pelo `video_player` | Live só oferece HLS, que o `video_player` descarta | O plugin resolve o HLS sozinho |

**Console poluído:** enquanto uma câmera toca, o FFmpeg escreve linhas como `[hls @ …] Opening …` a cada segmento. É só log e não afeta nada: o mesmo acontece com os vídeos do YouTube que o Jarvis já tocava.

---

## Limitações

- **Sem som** nas câmeras do YouTube (o formato HLS ao vivo só tem imagem).
- **Posição aproximada**: os pinos do YouTube ficam no lugar citado no título (bairro, praia, cidade), não no endereço exato da câmera.
- **Só lugares conhecidos ou clicados**: uma câmera de um lugar fora da lista só entra no mapa depois que alguém clica ou pesquisa esse lugar.
- **Resultados misturados**: a busca às vezes inclui câmeras de outros lugares no fim da lista (em Copacabana vieram Los Angeles e Sydney). As relevantes aparecem primeiro.
- **Lugares pequenos** podem não ter nenhuma live. Tente a cidade grande mais próxima ou um ponto turístico em "Ir para…".
- **Contornos simplificados**: os países usam a escala 110m (leve); com muito zoom, a borda passa pelo mar. Por isso ela fica bem apagada no nível de cidade.
- **Lives somem**: uma transmissão pode sair do ar entre a busca e o clique. O painel mostra "Esta transmissão não está mais ao vivo."
- **Internet obrigatória.**

---

## O que foi testado

| Teste | Resultado |
|---|---|
| Busca e reprodução por voz simulada (Copacabana, próxima, lista Paris, escolher 2ª, aleatório, lugar inexistente) | ✅ |
| Qt tocando live HLS (Shibuya 720p) | ✅ 227 quadros em 12 s |
| Mapa no navegador: América do Sul → Brasil → clique perto do Rio | ✅ Identificou Itaboraí e listou câmeras do Rio |
| "Ir para… Copacabana" | ✅ |
| Clique numa câmera → vídeo enviado ao HUD (simulado) | ✅ |
| API sem token / com token errado | ✅ Recusada (403) |
| **HUD real**: abrir mapa no Japão → tocar Shibuya → fechar vídeo → voltar ao mapa → fechar mapa → avatar | ✅ Após a correção do OpenGL |
| Comandos por **voz com o Gemini** de verdade | ❌ Não testado |

O ponto mais provável de precisar de ajuste é o Gemini escolher este plugin em vez do `video_player`. A descrição do plugin pede isso explicitamente, mas isso só se confirma usando de verdade.

---

## Player de vídeo (todas as câmeras e vídeos)

O vídeo não ocupa mais o HUD inteiro: abre numa **janela menor** no canto inferior direito, por cima do HUD ou do mapa.

- **Cabeçalho:** som liga/desliga, **⛶ MAXIMIZAR / ❐ RESTAURAR** e **✕ CLOSE**. Arrastar pelo cabeçalho move a janela; duplo clique maximiza ou restaura.
- **Controles:** ⏪ 10 s, ▶/⏸, 10 s ⏩, barra de tempo arrastável com tempo decorrido / total e volume. Em transmissões ao vivo a barra fica desativada e aparece "● AO VIVO".
- Subir o volume liga o som, se estava mudo.

## Como desfazer

1. Apague `plugins/world_cams.py` e a pasta `plugins/_worldcams/`.
2. Restaure o `ui.py` original:

   Não há cópia do `ui.py` anterior nesta máquina: a tela web e a janela de vídeo teriam que ser retiradas à mão.

3. Opcional: remova a linha `PyQt6-WebEngine` do `requirements.txt` e desinstale o pacote:

   ```bash
   python -m pip uninstall PyQt6-WebEngine PyQt6-WebEngine-Qt6
   ```

4. Opcional: apague `memory/worldcams_countries.geojson`.

Também dá para só **desativar** o plugin pelo gerenciador de plugins do Jarvis, sem apagar nada.
