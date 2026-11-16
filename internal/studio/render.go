package studio

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type Receipt struct {
	Schema    int     `json:"schema_version"`
	Revision  string  `json:"revision"`
	SHA       string  `json:"sha256"`
	Duration  float64 `json:"expected_duration"`
	Preview   bool    `json:"preview"`
	Profile   string  `json:"profile"`
	Measured  Info    `json:"measured"`
	Elapsed   float64 `json:"elapsed_seconds"`
	Plan      Plan    `json:"plan"`
	SourceSHA string  `json:"source_sha256"`
}

func Render(m Media, s *Store, p Plan, profile string, preview bool) (string, error) {
	if e := p.Validate(); e != nil {
		return "", e
	}
	if !profileOK(profile) {
		return "", errors.New("unknown profile")
	}
	key, e := Hash(struct {
		Plan    Plan
		Profile string
		Preview bool
	}{p, profile, preview})
	if e != nil {
		return "", e
	}
	unlock, e := Lock(s.Root, "render-"+key)
	if e != nil {
		return "", e
	}
	defer unlock()
	source, e := filepath.Abs(p.Source)
	if e != nil {
		return "", e
	}
	info, e := m.Probe(source)
	if e != nil {
		return "", e
	}
	if !info.Audio {
		return "", errors.New("source has no audio")
	}
	for _, seg := range p.Segments {
		if seg.End > info.Duration+.001 {
			return "", errors.New("segment outside source")
		}
	}
	sourceSHA, e := s.SourceHash(source)
	if e != nil {
		return "", e
	}
	var sceneMap SceneMap
	if e = ReadJSON(p.SceneMap, &sceneMap); e != nil {
		return "", e
	}
	if e = sceneMap.Validate(sourceSHA, info.Duration); e != nil {
		return "", e
	}
	musicSHA := ""
	if p.Music != nil {
		musicSHA, e = HashFile(p.Music.Path)
		if e != nil {
			return "", e
		}
	}
	revision, e := Hash(struct {
		Plan                                  Plan
		SourceSHA, MusicSHA, Design, Renderer string
		Preview                               bool
		Profile                               string
	}{p, sourceSHA, musicSHA, DesignVersion, "go-ffmpeg-1", preview, profile})
	if e != nil {
		return "", e
	}
	job := filepath.Join(s.Root, "renders", p.ID, revision)
	if e = os.MkdirAll(job, 0755); e != nil {
		return "", e
	}
	file := filepath.Join(job, "video.mp4")
	receiptPath := filepath.Join(job, "receipt.json")
	var old Receipt
	if ReadJSON(receiptPath, &old) == nil && old.Schema == 1 {
		if sha, err := HashFile(file); err == nil && sha == old.SHA && old.Revision == revision {
			return file, nil
		}
	}
	width, height := 1080, 1920
	if preview {
		width, height = 540, 960
	}
	ass, e := Captions(p, profile, width, height)
	if e != nil {
		return "", e
	}
	if e = AtomicWrite(filepath.Join(job, "captions.ass"), []byte(ass)); e != nil {
		return "", e
	}
	for _, name := range []string{"Montserrat.ttf", "Montserrat-Italic.ttf"} {
		b, err := Fonts.ReadFile("fonts/" + name)
		if err != nil {
			return "", err
		}
		if e = AtomicWrite(filepath.Join(job, name), b); e != nil {
			return "", e
		}
	}
	args := []string{"-y"}
	filters := []string{}
	links := []string{}
	for i, seg := range p.Segments {
		args = append(args, "-ss", f(seg.Start), "-t", f(seg.End-seg.Start), "-i", source)
		if profile == "cinema" {
			filters = append(filters, fmt.Sprintf("[%d:v:0]setpts=PTS-STARTPTS,scale=%d:%d:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=0x101216,setsar=1[v%d]", i, width, height, width, height, i))
		} else {
			filters = append(filters, fmt.Sprintf("[%d:v:0]setpts=PTS-STARTPTS,split=2[fgin%d][bgin%d]", i, i, i),
				fmt.Sprintf("[bgin%d]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=12:1,scale=%d:%d,setsar=1[bg%d]", i, width, height, i),
				fmt.Sprintf("[fgin%d]scale=%d:%d:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[fg%d]", i, width, height, i),
				fmt.Sprintf("[bg%d][fg%d]overlay=(W-w)/2:(H-h)/2[v%d]", i, i, i))
		}
		filters = append(filters, fmt.Sprintf("[%d:a:%d]atrim=duration=%s,asetpts=PTS-STARTPTS,aresample=48000[a%d]", i, p.AudioTrack, f(seg.End-seg.Start), i))
		links = append(links, fmt.Sprintf("[v%d][a%d]", i, i))
	}
	filters = append(filters, strings.Join(links, "")+fmt.Sprintf("concat=n=%d:v=1:a=1[vjoin][voice]", len(links)))
	effects, current := EffectFilters(p.Effects, width, height, info.FPS)
	filters = append(filters, effects...)
	filters = append(filters, fmt.Sprintf("[%s]ass=captions.ass:fontsdir=.[vout]", current))
	voice := "voice"
	if p.Music != nil {
		musicPath, err := filepath.Abs(p.Music.Path)
		if err != nil {
			return "", err
		}
		args = append(args, "-stream_loop", "-1", "-i", musicPath)
		filters = append(filters, "[voice]asplit=2[main][key]", fmt.Sprintf("[%d:a:0]atrim=duration=%s,asetpts=PTS-STARTPTS,volume=0.12[bed]", len(p.Segments), f(p.Duration())), "[bed][key]sidechaincompress=threshold=0.008:ratio=8:attack=10:release=250[duck]", "[main][duck]amix=inputs=2:duration=first:normalize=0[mix]")
		voice = "mix"
	}
	filters = append(filters, fmt.Sprintf("[%s]loudnorm=I=-16:TP=-3:LRA=9,aresample=48000[aout]", voice))
	preset, crf := "fast", "18"
	if preview {
		preset, crf = "veryfast", "25"
	}
	args = append(args, "-filter_complex", strings.Join(filters, ";"), "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", f(p.Duration()), "rendering.mp4")
	start := time.Now()
	log, e := m.Run(job, time.Hour, args...)
	if err := AtomicWrite(filepath.Join(job, "render.log"), []byte(log)); e == nil && err != nil {
		e = err
	}
	if e != nil {
		return "", e
	}
	temporary := filepath.Join(job, "rendering.mp4")
	measured, e := m.Probe(temporary)
	if e != nil {
		return "", e
	}
	if abs(measured.Duration-p.Duration()) > math.Max(.15, 2/info.FPS) {
		return "", errors.New("export duration mismatch")
	}
	if e = os.Rename(temporary, file); e != nil {
		return "", e
	}
	sha, e := HashFile(file)
	if e != nil {
		return "", e
	}
	receipt := Receipt{Schema: 1, Revision: revision, SHA: sha, Duration: p.Duration(), Preview: preview, Profile: profile, Measured: measured, Elapsed: time.Since(start).Seconds(), Plan: p, SourceSHA: sourceSHA}
	return file, WriteJSON(receiptPath, receipt)
}
func f(n float64) string { return strconv.FormatFloat(n, 'f', 6, 64) }

var loudnessRE = regexp.MustCompile(`(?s)\{\s*"input_i".*?\}`)
var blackRE = regexp.MustCompile(`black_start:[^\r\n]+`)
var freezeRE = regexp.MustCompile(`freeze_start:[^\r\n]+`)

func decodeLoudness(s string, v map[string]string) error { return json.Unmarshal([]byte(s), &v) }
func Check(m Media, s *Store, file string) (Quality, error) {
	var receipt Receipt
	var q Quality
	if e := ReadJSON(filepath.Join(filepath.Dir(file), "receipt.json"), &receipt); e != nil {
		return q, e
	}
	if receipt.Preview {
		return q, errors.New("preview cannot be approved")
	}
	if receipt.Schema != 1 || receipt.Plan.Validate() != nil || abs(receipt.Duration-receipt.Plan.Duration()) > .001 || len(receipt.SourceSHA) != 64 || len(receipt.Revision) != 64 {
		return q, errors.New("foreign or invalid export receipt; render with the Go pipeline first")
	}
	sha, e := HashFile(file)
	if e != nil {
		return q, e
	}
	if sha != receipt.SHA {
		return q, errors.New("export changed since render")
	}
	q.SHA = sha
	q.Demo = receipt.Plan.Demonstration
	started := time.Now()
	log, e := m.Run("", time.Hour, "-xerror", "-err_detect", "explode", "-i", file, "-vf", "blackdetect=d=0.2:pix_th=0.1,freezedetect=n=-60dB:d=2", "-af", "loudnorm=I=-16:TP=-3:LRA=9:print_format=json", "-f", "null", "-")
	if e != nil {
		_ = s.Invalidate(sha, q)
		return q, e
	}
	if e = AtomicWrite(filepath.Join(filepath.Dir(file), "qa.log"), []byte(log)); e != nil {
		return q, e
	}
	q.FullDecode = true
	q.Black = blackRE.FindAllString(log, -1)
	q.Freeze = freezeRE.FindAllString(log, -1)
	q.Loudness = map[string]string{}
	matches := loudnessRE.FindAllString(log, -1)
	if len(matches) > 0 {
		if e = decodeLoudness(matches[len(matches)-1], q.Loudness); e != nil {
			return q, e
		}
	}
	info, e := m.Probe(file)
	if e != nil {
		return q, e
	}
	q.Duration = info.Duration
	q.Elapsed = time.Since(started).Seconds()
	level, e1 := strconv.ParseFloat(q.Loudness["input_i"], 64)
	peak, e2 := strconv.ParseFloat(q.Loudness["input_tp"], 64)
	q.Passed = e1 == nil && e2 == nil && finite(level) && finite(peak) && level >= -35 && level <= -5 && peak <= 0 && len(q.Black) == 0 && len(q.Freeze) == 0 && abs(info.Duration-receipt.Duration) <= .15
	if info.Duration > 60 {
		q.Warnings = append(q.Warnings, "Longer than 60 seconds: check actual YouTube restrictions")
	}
	if e = WriteJSON(filepath.Join(filepath.Dir(file), "qa.json"), q); e != nil {
		return q, e
	}
	if q.Passed && !q.Demo {
		story, err := Hash(struct {
			Source   string
			Segments []Segment
		}{receipt.SourceSHA, receipt.Plan.Segments})
		if err != nil {
			return q, err
		}
		e = s.Register(file, story, receipt.Revision, q)
	} else if !q.Passed {
		e = s.Invalidate(sha, q)
	}
	return q, e
}
