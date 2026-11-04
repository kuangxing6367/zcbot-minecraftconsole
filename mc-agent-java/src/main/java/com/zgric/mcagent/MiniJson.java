package com.zgric.mcagent;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 编解码（Java 8，零依赖）。
 *
 * 为什么不用 Gson：agent 是打包发布的 jar，运行期只保证 Bukkit 1.12.2 自带的类；
 * 把协议层做成零依赖，可以避免不同服务端发行版里 JSON 库缺失/版本差异导致
 * NoClassDefFoundError。本类只覆盖 RPC envelope 需要的类型子集：
 * object / array / string / number / boolean / null。
 */
public final class MiniJson {

    private MiniJson() {}

    public static String stringify(Object value) {
        StringBuilder sb = new StringBuilder();
        write(sb, value);
        return sb.toString();
    }

    private static void write(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String) {
            writeString(sb, (String) v);
        } else if (v instanceof Boolean || v instanceof Number) {
            sb.append(v.toString());
        } else if (v instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Object entryObj : ((Map<?, ?>) v).entrySet()) {
                Map.Entry<?, ?> e = (Map.Entry<?, ?>) entryObj;
                if (!first) sb.append(',');
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                write(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof Iterable) {
            sb.append('[');
            boolean first = true;
            for (Object item : (Iterable<?>) v) {
                if (!first) sb.append(',');
                first = false;
                write(sb, item);
            }
            sb.append(']');
        } else {
            writeString(sb, String.valueOf(v));
        }
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':  sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\b': sb.append("\\b"); break;
                case '\f': sb.append("\\f"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
    }

    public static Object parse(String text) {
        Parser p = new Parser(text);
        Object v = p.value();
        p.skipWs();
        if (!p.end()) throw new IllegalArgumentException("trailing characters in JSON");
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) throw new IllegalArgumentException("JSON root is not an object");
        return (Map<String, Object>) v;
    }

    private static final class Parser {
        private final String s;
        private int i;

        Parser(String s) { this.s = s; }

        boolean end() { return i >= s.length(); }

        void skipWs() {
            while (i < s.length()) {
                char c = s.charAt(i);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') i++;
                else break;
            }
        }

        Object value() {
            skipWs();
            if (end()) throw err("unexpected end");
            char c = s.charAt(i);
            switch (c) {
                case '{': return object();
                case '[': return array();
                case '"': return string();
                case 't': expect("true");  return Boolean.TRUE;
                case 'f': expect("false"); return Boolean.FALSE;
                case 'n': expect("null");  return null;
                default:
                    if (c == '-' || (c >= '0' && c <= '9')) return number();
                    throw err("unexpected char '" + c + "'");
            }
        }

        private void expect(String literal) {
            if (!s.regionMatches(i, literal, 0, literal.length())) throw err("expected " + literal);
            i += literal.length();
        }

        private Map<String, Object> object() {
            Map<String, Object> m = new LinkedHashMap<String, Object>();
            i++; // {
            skipWs();
            if (!end() && s.charAt(i) == '}') { i++; return m; }
            while (true) {
                skipWs();
                if (end() || s.charAt(i) != '"') throw err("expected object key");
                String key = string();
                skipWs();
                if (end() || s.charAt(i) != ':') throw err("expected ':'");
                i++;
                m.put(key, value());
                skipWs();
                if (end()) throw err("unterminated object");
                char c = s.charAt(i++);
                if (c == '}') return m;
                if (c != ',') throw err("expected ',' or '}'");
            }
        }

        private List<Object> array() {
            List<Object> a = new ArrayList<Object>();
            i++; // [
            skipWs();
            if (!end() && s.charAt(i) == ']') { i++; return a; }
            while (true) {
                a.add(value());
                skipWs();
                if (end()) throw err("unterminated array");
                char c = s.charAt(i++);
                if (c == ']') return a;
                if (c != ',') throw err("expected ',' or ']'");
            }
        }

        private String string() {
            i++; // opening quote
            StringBuilder sb = new StringBuilder();
            while (!end()) {
                char c = s.charAt(i++);
                if (c == '"') return sb.toString();
                if (c != '\\') { sb.append(c); continue; }
                if (end()) throw err("dangling escape");
                char e = s.charAt(i++);
                switch (e) {
                    case '"':  sb.append('"');  break;
                    case '\\': sb.append('\\'); break;
                    case '/':  sb.append('/');  break;
                    case 'b':  sb.append('\b'); break;
                    case 'f':  sb.append('\f'); break;
                    case 'n':  sb.append('\n'); break;
                    case 'r':  sb.append('\r'); break;
                    case 't':  sb.append('\t'); break;
                    case 'u':
                        if (i + 4 > s.length()) throw err("bad unicode escape");
                        sb.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                        i += 4;
                        break;
                    default: throw err("bad escape \\" + e);
                }
            }
            throw err("unterminated string");
        }

        private Number number() {
            int start = i;
            if (s.charAt(i) == '-') i++;
            while (!end() && Character.isDigit(s.charAt(i))) i++;
            boolean decimal = false;
            if (!end() && s.charAt(i) == '.') {
                decimal = true;
                i++;
                while (!end() && Character.isDigit(s.charAt(i))) i++;
            }
            if (!end() && (s.charAt(i) == 'e' || s.charAt(i) == 'E')) {
                decimal = true;
                i++;
                if (!end() && (s.charAt(i) == '+' || s.charAt(i) == '-')) i++;
                while (!end() && Character.isDigit(s.charAt(i))) i++;
            }
            String raw = s.substring(start, i);
            try {
                return decimal ? (Number) Double.valueOf(raw) : (Number) Long.valueOf(raw);
            } catch (NumberFormatException ex) {
                throw err("bad number " + raw);
            }
        }

        private IllegalArgumentException err(String msg) {
            return new IllegalArgumentException("JSON at " + i + ": " + msg);
        }
    }
}
